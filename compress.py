"""
Compression logic for Assante Statement PDFs (web version).

Extracted from compress_assante_statements.py. Compresses PDFs using
JBIG2 encoding for large merger-notice images, achieving ~93% reduction.
"""

import io
import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Optional

import pikepdf
from pikepdf import Name, Dictionary
from PIL import Image


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GS_COMMAND = "gs"
JBIG2_COMMAND = "jbig2"

GS_PRE_ARGS = [
    "-sDEVICE=pdfwrite",
    "-dCompatibilityLevel=1.7",
    "-dNOPAUSE",
    "-dBATCH",
    "-dQUIET",
    "-dDownsampleColorImages=true",
    "-dColorImageDownsampleType=/Bicubic",
    "-dColorImageResolution=150",
    "-dDownsampleGrayImages=true",
    "-dGrayImageDownsampleType=/Bicubic",
    "-dGrayImageResolution=150",
    "-dDownsampleMonoImages=true",
    "-dMonoImageDownsampleType=/Subsample",
    "-dMonoImageResolution=300",
    "-dAutoFilterColorImages=false",
    "-dColorImageFilter=/DCTEncode",
    "-dAutoFilterGrayImages=false",
    "-dGrayImageFilter=/DCTEncode",
    "-dPreserveAnnots=true",
    "-dPreserveOPIComments=false",
    "-dOptimize=true",
    "-dCompressFonts=true",
    "-dSubsetFonts=true",
    "-dEmbedAllFonts=true",
    "-dPassThroughJPEGImages=false",
    "-dEncodeColorImages=true",
    "-dEncodeGrayImages=true",
    "-dEncodeMonoImages=true",
]

GS_PS_PARAMS = [
    "-c",
    "<< /ColorACSImageDict << /QFactor 0.40 /Blend 1 /HSamples [1 1 1 1] /VSamples [1 1 1 1] >> >> setdistillerparams",
    "<< /GrayACSImageDict  << /QFactor 0.40 /Blend 1 /HSamples [1 1 1 1] /VSamples [1 1 1 1] >> >> setdistillerparams",
    "-f",
]

JBIG2_MIN_WIDTH = 2000
JBIG2_MIN_HEIGHT = 3000
JBIG2_MIN_RAW_BYTES = 100_000
MONO_THRESHOLD = 128
MIN_COMPRESSION_RATIO = 0.10
MIN_ORIGINAL_SIZE_BYTES = 1_000_000  # 1 MB


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PdfResult:
    filename: str
    original_size: int
    compressed_size: int
    skipped: bool = False
    error: str = ""


@dataclass
class ZipResult:
    zip_name: str
    total_pdfs: int = 0
    compressed_pdfs: int = 0
    skipped_pdfs: int = 0
    failed_pdfs: int = 0
    original_total_bytes: int = 0
    compressed_total_bytes: int = 0
    pdf_results: list = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

def check_dependencies() -> Optional[str]:
    """Return an error message if dependencies are missing, or None if OK."""
    for cmd, pkg in [(GS_COMMAND, "ghostscript"), (JBIG2_COMMAND, "jbig2enc")]:
        if shutil.which(cmd) is None:
            return f"{cmd} not found. Install {pkg}."
    return None


# ---------------------------------------------------------------------------
# Pass 1: JBIG2 conversion of large merger-page images
# ---------------------------------------------------------------------------

def _convert_large_images_to_jbig2(pdf_path: str, tmp_dir: str) -> int:
    """
    Open a PDF, find large full-page images (merger notices), convert
    them from RGB JPEG to monochrome JBIG2 at original resolution.
    Saves the modified PDF in-place.  Returns the number of images converted.
    """
    pdf = pikepdf.Pdf.open(pdf_path, allow_overwriting_input=True)
    converted = 0

    for page in pdf.pages:
        resources = page.get("/Resources", {})
        xobjects = resources.get("/XObject", {})
        if not xobjects:
            continue

        for img_name in list(xobjects.keys()):
            img_obj = xobjects[img_name]
            if not hasattr(img_obj, "keys"):
                continue
            if str(img_obj.get("/Subtype", "")) != "/Image":
                continue

            w = int(img_obj.get("/Width", 0))
            h = int(img_obj.get("/Height", 0))
            raw_compressed = bytes(img_obj.read_raw_bytes())

            if (w < JBIG2_MIN_WIDTH or h < JBIG2_MIN_HEIGHT
                    or len(raw_compressed) < JBIG2_MIN_RAW_BYTES):
                continue

            filt = str(img_obj.get("/Filter", ""))
            if "DCTDecode" not in filt:
                continue

            try:
                img = Image.open(io.BytesIO(raw_compressed))
                gray = img.convert("L")
                mono = gray.point(lambda x: 0 if x < MONO_THRESHOLD else 255, "1")

                pbm_path = os.path.join(tmp_dir, f"jbig2_{converted}.pbm")
                mono.save(pbm_path)

                subprocess.run(
                    [JBIG2_COMMAND, "-p", "-s", pbm_path],
                    capture_output=True,
                    cwd=tmp_dir,
                    timeout=60,
                )

                page_file = os.path.join(tmp_dir, "output.0000")
                sym_file = os.path.join(tmp_dir, "output.sym")

                if not os.path.exists(page_file):
                    continue

                with open(page_file, "rb") as f:
                    jbig2_page_data = f.read()
                with open(sym_file, "rb") as f:
                    jbig2_globals = f.read()

                globals_stream = pdf.make_stream(jbig2_globals)

                new_stream = pdf.make_stream(jbig2_page_data)
                new_stream[Name.Type] = Name.XObject
                new_stream[Name.Subtype] = Name.Image
                new_stream[Name.Width] = w
                new_stream[Name.Height] = h
                new_stream[Name.ColorSpace] = Name.DeviceGray
                new_stream[Name.BitsPerComponent] = 1
                new_stream[Name.Filter] = Name.JBIG2Decode
                new_stream[Name.DecodeParms] = Dictionary(
                    JBIG2Globals=globals_stream
                )

                xobjects[img_name] = new_stream
                converted += 1

                for fname in ["output.0000", "output.sym"]:
                    p = os.path.join(tmp_dir, fname)
                    if os.path.exists(p):
                        os.remove(p)
                if os.path.exists(pbm_path):
                    os.remove(pbm_path)

            except Exception:
                continue

    pdf.save(pdf_path)
    pdf.close()
    return converted


# ---------------------------------------------------------------------------
# Core compression (two-pass)
# ---------------------------------------------------------------------------

def compress_single_pdf(src_path: str, dst_path: str) -> PdfResult:
    """
    Compress one PDF using JBIG2 encoding for large merger images.
    """
    filename = os.path.basename(src_path)
    original_size = os.path.getsize(src_path)

    if original_size < MIN_ORIGINAL_SIZE_BYTES:
        shutil.copy2(src_path, dst_path)
        return PdfResult(
            filename=filename,
            original_size=original_size,
            compressed_size=original_size,
            skipped=True,
        )

    try:
        with tempfile.TemporaryDirectory(prefix="pdf_jbig2_") as tmp_dir:
            work_path = os.path.join(tmp_dir, "work.pdf")
            shutil.copy2(src_path, work_path)

            _convert_large_images_to_jbig2(work_path, tmp_dir)

            final_path = work_path
            compressed_size = os.path.getsize(final_path)

            if compressed_size >= original_size * (1 - MIN_COMPRESSION_RATIO):
                shutil.copy2(src_path, dst_path)
                return PdfResult(
                    filename=filename,
                    original_size=original_size,
                    compressed_size=original_size,
                    skipped=True,
                )

            with open(final_path, "rb") as f:
                header = f.read(5)
            if header != b"%PDF-":
                shutil.copy2(src_path, dst_path)
                return PdfResult(
                    filename=filename,
                    original_size=original_size,
                    compressed_size=original_size,
                    error="Compressed output is not a valid PDF",
                )

            shutil.copy2(final_path, dst_path)
            return PdfResult(
                filename=filename,
                original_size=original_size,
                compressed_size=compressed_size,
            )

    except subprocess.TimeoutExpired:
        shutil.copy2(src_path, dst_path)
        return PdfResult(
            filename=filename,
            original_size=original_size,
            compressed_size=original_size,
            error="Processing timed out",
        )
    except Exception as e:
        shutil.copy2(src_path, dst_path)
        return PdfResult(
            filename=filename,
            original_size=original_size,
            compressed_size=original_size,
            error=str(e),
        )


# ---------------------------------------------------------------------------
# ZIP processor for web
# ---------------------------------------------------------------------------

# Type alias for progress callback: (current_index, total_count, filename)
ProgressCallback = Callable[[int, int, str], None]


def process_zip_for_web(
    input_path: str,
    output_path: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> ZipResult:
    """
    Extract all PDFs from a ZIP, compress each, and write a new compressed
    ZIP to output_path. Calls progress_callback after each PDF is processed.
    """
    zip_name = os.path.basename(input_path)
    result = ZipResult(zip_name=zip_name)

    try:
        with tempfile.TemporaryDirectory(prefix="pdf_extract_") as extract_dir, \
             tempfile.TemporaryDirectory(prefix="pdf_compressed_") as compress_dir:

            with zipfile.ZipFile(input_path, "r") as zf:
                all_entries = zf.namelist()
                pdf_entries = [e for e in all_entries if e.lower().endswith(".pdf")]
                non_pdf_entries = [e for e in all_entries if not e.lower().endswith(".pdf")]
                result.total_pdfs = len(pdf_entries)
                zf.extractall(extract_dir)

            for i, entry in enumerate(pdf_entries):
                src = os.path.join(extract_dir, entry)
                dst = os.path.join(compress_dir, entry)
                os.makedirs(os.path.dirname(dst), exist_ok=True)

                if not os.path.isfile(src):
                    result.failed_pdfs += 1
                    result.pdf_results.append(PdfResult(
                        filename=entry, original_size=0,
                        compressed_size=0, error="File not found after extraction",
                    ))
                    if progress_callback:
                        progress_callback(i + 1, len(pdf_entries), entry)
                    continue

                pdf_result = compress_single_pdf(src, dst)
                result.pdf_results.append(pdf_result)
                result.original_total_bytes += pdf_result.original_size
                result.compressed_total_bytes += pdf_result.compressed_size

                if pdf_result.error:
                    result.failed_pdfs += 1
                elif pdf_result.skipped:
                    result.skipped_pdfs += 1
                else:
                    result.compressed_pdfs += 1

                if progress_callback:
                    progress_callback(i + 1, len(pdf_entries), entry)

            # Write compressed ZIP to output_path
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf_out:
                for entry in pdf_entries:
                    compressed_file = os.path.join(compress_dir, entry)
                    if os.path.isfile(compressed_file):
                        zf_out.write(compressed_file, entry)

                for entry in non_pdf_entries:
                    src = os.path.join(extract_dir, entry)
                    if os.path.isfile(src):
                        zf_out.write(src, entry)

    except Exception as e:
        result.error = str(e)

    return result
