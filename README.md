# PDF Compression Tool

Compresses large statement PDFs by up to 93%. A 7 MB PDF becomes ~461 KB while keeping text perfectly readable.

## How It Works

Each page in these PDFs is stored as a full-color photograph, even though the content is just black text on a white page. This tool:

1. Opens each PDF and finds the large page images inside
2. Converts each image from full color to black and white
3. Compresses using JBIG2 — a format that recognizes repeated letters (e.g., every "e" on a page looks the same, so it stores it once and reuses it)
4. Puts the compressed images back into the PDF

## Prerequisites

### macOS

```bash
brew install ghostscript jbig2enc
pip install pikepdf Pillow
```

### Ubuntu / Debian

```bash
sudo apt install ghostscript
pip install pikepdf Pillow
```

For jbig2enc on Ubuntu, build from source:

```bash
sudo apt install build-essential automake autoconf libtool pkg-config libleptonica-dev zlib1g-dev libpng-dev git
git clone https://github.com/agl/jbig2enc.git
cd jbig2enc
./autogen.sh
./configure
make
sudo make install
sudo ldconfig
```

### Windows

1. Install [Ghostscript](https://www.ghostscript.com/releases/gsdnld.html) and add it to your PATH
2. Build jbig2enc from source or use a prebuilt binary
3. Install Python packages:

```bash
pip install pikepdf Pillow
```

## Usage

### Basic usage

Place your ZIP files containing PDFs in a folder, then run:

```bash
python3 compress_assante_statements.py /path/to/your/zip/folder
```

The script will:
- Find all ZIP files in the folder
- Extract PDFs from each ZIP
- Compress every PDF
- Repack the compressed PDFs back into the original ZIP (overwrites in place)

### Options

```bash
# Use 4 parallel workers (default)
python3 compress_assante_statements.py /path/to/folder --workers 4

# Use 8 workers for faster processing
python3 compress_assante_statements.py /path/to/folder --workers 8

# Preview what would be compressed without making changes
python3 compress_assante_statements.py /path/to/folder --dry-run

# Save logs to a specific directory
python3 compress_assante_statements.py /path/to/folder --log-dir /path/to/logs
```

### Example

```bash
$ python3 compress_assante_statements.py ./test_run/ --workers 4

11:28:57  Found 1 ZIP file(s) in ./test_run
11:28:57  Workers: 4 | Dry run: False
11:44:23  [1/1] B2A_P_B2AAAPLG_S2025100922062701.zip  |  PDFs: 1403  compressed: 1403  skipped: 0  failed: 0  |  9,681.0 MB -> 677.1 MB  (93.0% reduction)
11:44:23  COMPRESSION COMPLETE
11:44:23    Elapsed:      15.4 minutes
11:44:23    ZIP files:    1 total, 0 failed
11:44:23    PDFs:         1403 total
11:44:23    Size:         9.45 GB -> 0.66 GB  (93.0% reduction)
```

## Libraries Used

- **pikepdf** — opens PDFs and accesses the images inside them
- **Pillow** — converts color images to black and white
- **jbig2enc** — compresses black-and-white images (where the main size reduction happens)
- **Ghostscript** — PDF processing engine (available for additional compression)

## Notes

- The script overwrites the original ZIP files with compressed versions. Make a backup first if needed.
- PDFs smaller than 1 MB are skipped (they don't contain the large uncompressed images).
- If compression doesn't reduce size by at least 10%, the original PDF is kept.
- Log files are saved to a `logs` folder inside your ZIP directory by default.
