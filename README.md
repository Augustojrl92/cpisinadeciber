# Arachnida

Implementation of the mandatory part of the 42 Cybersecurity `Arachnida` project.

It contains two independent command-line programs:

- `spider`: fetches a web page, extracts supported image URLs, and optionally follows links on the same origin.
- `scorpion`: displays basic image attributes and EXIF or format metadata for one or more image files.

The HTTP requests, HTML link extraction, crawl queue, duplicate handling, and output naming are implemented in this project. It does not use `wget` or `scrapy`, which the subject explicitly forbids.

## Requirements

- Python 3.10 or newer; no external Python packages are required.

## Spider

Extract images referenced by one page:

```sh
./spider https://example.org/gallery
```

Follow pages on the same website, with the subject's default maximum depth of 5:

```sh
./spider -r https://example.org/gallery
```

Set an explicit maximum depth and output directory:

```sh
./spider -r -l 2 -p ./images https://example.org/gallery
```

The supported extensions are `.jpg`, `.jpeg`, `.png`, `.gif`, and `.bmp`. The default output directory is `./data/`. Recursive page traversal stays on the starting origin; image URLs referenced by those pages may use another host.

## Scorpion

Inspect one or more images:

```sh
./scorpion ./data/photo.jpg
./scorpion ./data/photo.jpg ./data/diagram.png
```

The output includes format, dimensions, file size, an available creation date, EXIF tags, and other metadata exposed by the image format. A missing EXIF block is reported rather than treated as an error.

## Implementation notes

- Spider uses Python's `urllib` for HTTP and `html.parser` for page parsing. The crawling and filtering logic is written here.
- Recursive page links are restricted to the starting website's origin, preventing the crawler from wandering across unrelated sites.
- Images are deduplicated by URL. If two distinct URLs have the same filename, the later file receives a numeric suffix.
- Scorpion parses JPEG EXIF/TIFF structures and common PNG, GIF, and BMP metadata using the Python standard library.

The optional subject bonuses (editing/removing metadata and a graphical interface) are not included in this mandatory-part starter.
