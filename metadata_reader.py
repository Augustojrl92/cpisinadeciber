"""Small, dependency-free readers for metadata in the required image formats.

The parser implements the metadata traversal itself instead of delegating EXIF
extraction to an image-processing package.
"""

from __future__ import annotations

from pathlib import Path
import struct
import zlib


class ImageMetadataError(Exception):
    """Raised when an image or one of its metadata blocks is malformed."""


EXIF_TAGS = {
    0x0100: "ImageWidth", 0x0101: "ImageLength", 0x0102: "BitsPerSample",
    0x0103: "Compression", 0x0106: "PhotometricInterpretation",
    0x010E: "ImageDescription", 0x010F: "Make", 0x0110: "Model",
    0x0112: "Orientation", 0x0115: "SamplesPerPixel", 0x0116: "RowsPerStrip",
    0x0117: "StripByteCounts", 0x011A: "XResolution", 0x011B: "YResolution",
    0x0128: "ResolutionUnit", 0x0131: "Software", 0x0132: "DateTime",
    0x013B: "Artist", 0x0213: "YCbCrPositioning", 0x8298: "Copyright",
    0x8769: "ExifIFDPointer", 0x8825: "GPSInfoIFDPointer",
}
SUBIFD_TAGS = {
    0x829A: "ExposureTime", 0x829D: "FNumber", 0x8822: "ExposureProgram",
    0x8827: "ISOSpeedRatings", 0x9000: "ExifVersion", 0x9003: "DateTimeOriginal",
    0x9004: "DateTimeDigitized", 0x9101: "ComponentsConfiguration",
    0x9102: "CompressedBitsPerPixel", 0x9201: "ShutterSpeedValue",
    0x9202: "ApertureValue", 0x9204: "ExposureBiasValue", 0x9207: "MeteringMode",
    0x9209: "Flash", 0x920A: "FocalLength", 0x927C: "MakerNote",
    0x9286: "UserComment", 0xA001: "ColorSpace", 0xA002: "PixelXDimension",
    0xA003: "PixelYDimension", 0xA405: "FocalLengthIn35mmFilm",
    0xA434: "LensModel", 0xA420: "ImageUniqueID", 0xA431: "BodySerialNumber",
}
GPS_TAGS = {
    0: "GPSVersionID", 1: "GPSLatitudeRef", 2: "GPSLatitude", 3: "GPSLongitudeRef",
    4: "GPSLongitude", 5: "GPSAltitudeRef", 6: "GPSAltitude", 7: "GPSTimeStamp",
    12: "GPSSpeedRef", 13: "GPSSpeed", 16: "GPSImgDirectionRef",
    17: "GPSImgDirection", 29: "GPSDateStamp",
}
TIFF_TYPES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _checked_slice(data: bytes, start: int, size: int) -> bytes:
    if start < 0 or size < 0 or start + size > len(data):
        raise ImageMetadataError("metadata block points outside the file")
    return data[start:start + size]


def _as_scalar(values):
    return values[0] if len(values) == 1 else values


def _decode_tiff_value(data: bytes, value_type: int, count: int, endian: str):
    if value_type not in TIFF_TYPES:
        return f"<unsupported TIFF type {value_type}>"
    item_size = TIFF_TYPES[value_type]
    total_size = item_size * count
    if total_size > len(data):
        raise ImageMetadataError("truncated TIFF value")

    if value_type == 2:
        return data[:total_size].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    if value_type in {1, 6}:
        return _as_scalar(list(data[:total_size]))
    if value_type == 7:
        raw = data[:total_size]
        printable = raw.rstrip(b"\x00")
        if printable and all(32 <= byte < 127 for byte in printable):
            return printable.decode("ascii", errors="replace")
        return "0x" + raw.hex()

    formats = {3: "H", 4: "I", 8: "h", 9: "i", 11: "f", 12: "d"}
    if value_type in formats:
        code = endian + formats[value_type] * count
        return _as_scalar(list(struct.unpack(code, data[:total_size])))
    if value_type in {5, 10}:
        code = endian + ("II" if value_type == 5 else "ii") * count
        raw = struct.unpack(code, data[:total_size])
        values = []
        for index in range(0, len(raw), 2):
            numerator, denominator = raw[index:index + 2]
            values.append(None if denominator == 0 else numerator / denominator)
        return _as_scalar(values)
    return f"<unsupported TIFF type {value_type}>"


def _parse_tiff(data: bytes) -> dict:
    if len(data) < 8:
        raise ImageMetadataError("TIFF/EXIF header is too short")
    byte_order = data[:2]
    if byte_order == b"II":
        endian = "<"
    elif byte_order == b"MM":
        endian = ">"
    else:
        raise ImageMetadataError("invalid TIFF byte order")
    magic, first_ifd = struct.unpack(endian + "HI", data[2:8])
    if magic != 42:
        raise ImageMetadataError("invalid TIFF header")

    def read_ifd(offset: int, tag_names: dict[int, str], level: int) -> dict:
        if level > 4 or offset == 0:
            return {}
        count = struct.unpack(endian + "H", _checked_slice(data, offset, 2))[0]
        if count > 4096:
            raise ImageMetadataError("unreasonable TIFF directory size")
        entries = {}
        cursor = offset + 2
        for _ in range(count):
            entry = _checked_slice(data, cursor, 12)
            tag_id, value_type, value_count = struct.unpack(endian + "HHI", entry[:8])
            byte_count = TIFF_TYPES.get(value_type, 1) * value_count
            if byte_count <= 4:
                value_data = entry[8:8 + byte_count]
            else:
                value_offset = struct.unpack(endian + "I", entry[8:12])[0]
                value_data = _checked_slice(data, value_offset, byte_count)
            value = _decode_tiff_value(value_data, value_type, value_count, endian)
            label = tag_names.get(tag_id, f"Tag 0x{tag_id:04X}")

            if tag_id in {0x8769, 0x8825, 0xA005} and isinstance(value, int):
                child_names = GPS_TAGS if tag_id == 0x8825 else SUBIFD_TAGS
                child = read_ifd(value, child_names, level + 1)
                if child:
                    entries["GPSInfo" if tag_id == 0x8825 else "ExifSubIFD"] = child
                else:
                    entries[label] = value
            else:
                entries[label] = value
            cursor += 12
        return entries

    return read_ifd(first_ifd, EXIF_TAGS, 0)


def _jpeg(data: bytes) -> dict:
    if not data.startswith(b"\xff\xd8"):
        raise ImageMetadataError("invalid JPEG signature")
    result = {"format": "JPEG", "metadata": {}}
    position = 2
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while position < len(data):
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in {0xD9, 0xDA}:
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(data):
            break
        segment_length = struct.unpack(">H", data[position:position + 2])[0]
        if segment_length < 2 or position + segment_length > len(data):
            raise ImageMetadataError("invalid JPEG segment length")
        payload = data[position + 2:position + segment_length]
        if marker in sof_markers and len(payload) >= 5:
            result["height"], result["width"] = struct.unpack(">HH", payload[1:5])
        elif marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            result["metadata"].update(_parse_tiff(payload[6:]))
        elif marker == 0xFE:
            result["metadata"]["JPEGComment"] = payload.decode("utf-8", errors="replace")
        position += segment_length
    result["creation_date"] = _find_date(result["metadata"])
    return result


def _png(data: bytes) -> dict:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ImageMetadataError("invalid PNG signature")
    result = {"format": "PNG", "metadata": {}}
    position = 8
    while position + 12 <= len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        kind = data[position + 4:position + 8]
        payload_start = position + 8
        payload = _checked_slice(data, payload_start, length)
        position = payload_start + length + 4  # skip CRC
        if kind == b"IHDR" and len(payload) >= 13:
            width, height, bit_depth, color_type = struct.unpack(">IIBB", payload[:10])
            result.update(width=width, height=height, bit_depth=bit_depth, color_type=color_type)
        elif kind == b"eXIf":
            result["metadata"].update(_parse_tiff(payload))
        elif kind == b"tEXt":
            key, _, value = payload.partition(b"\x00")
            result["metadata"][key.decode("latin-1", errors="replace")] = value.decode("latin-1", errors="replace")
        elif kind == b"zTXt":
            key, separator, rest = payload.partition(b"\x00")
            if separator and len(rest) >= 2 and rest[0] == 0:
                try:
                    value = zlib.decompress(rest[1:]).decode("utf-8", errors="replace")
                except zlib.error:
                    value = "<invalid compressed text>"
                result["metadata"][key.decode("latin-1", errors="replace")] = value
        elif kind == b"iTXt":
            parts = payload.split(b"\x00", 5)
            if len(parts) == 6:
                key, compressed, _method, _language, _translated, value = parts
                if compressed == b"\x01":
                    try:
                        value = zlib.decompress(value)
                    except zlib.error:
                        value = b"<invalid compressed text>"
                result["metadata"][key.decode("utf-8", errors="replace")] = value.decode("utf-8", errors="replace")
        elif kind == b"IEND":
            break
    result["creation_date"] = _find_date(result["metadata"])
    return result


def _gif_subblocks(data: bytes, position: int) -> tuple[bytes, int]:
    blocks = []
    while position < len(data):
        size = data[position]
        position += 1
        if size == 0:
            break
        blocks.append(_checked_slice(data, position, size))
        position += size
    return b"".join(blocks), position


def _gif(data: bytes) -> dict:
    if data[:6] not in {b"GIF87a", b"GIF89a"} or len(data) < 13:
        raise ImageMetadataError("invalid GIF header")
    width, height = struct.unpack("<HH", data[6:10])
    packed = data[10]
    position = 13
    if packed & 0x80:
        position += 3 * (2 ** ((packed & 0x07) + 1))
    result = {"format": data[:6].decode("ascii"), "width": width, "height": height, "metadata": {}}
    comments = []
    while position < len(data):
        introducer = data[position]
        position += 1
        if introducer == 0x3B:
            break
        if introducer == 0x21:
            if position >= len(data):
                break
            label = data[position]
            position += 1
            content, position = _gif_subblocks(data, position)
            if label == 0xFE and content:
                comments.append(content.decode("utf-8", errors="replace"))
        elif introducer == 0x2C:
            descriptor = _checked_slice(data, position, 9)
            local_packed = descriptor[8]
            position += 9
            if local_packed & 0x80:
                position += 3 * (2 ** ((local_packed & 0x07) + 1))
            position += 1  # LZW minimum code size
            _image_data, position = _gif_subblocks(data, position)
        else:
            raise ImageMetadataError("unexpected GIF block")
    if comments:
        result["metadata"]["Comments"] = comments
    result["creation_date"] = _find_date(result["metadata"])
    return result


def _bmp(data: bytes) -> dict:
    if len(data) < 26 or data[:2] != b"BM":
        raise ImageMetadataError("invalid BMP header")
    dib_size = struct.unpack("<I", data[14:18])[0]
    if dib_size == 12:  # BITMAPCOREHEADER
        width, height = struct.unpack("<HH", data[18:22])
        bits_per_pixel = struct.unpack("<H", data[24:26])[0]
    elif dib_size >= 40 and len(data) >= 30:
        width, signed_height = struct.unpack("<ii", data[18:26])
        height = abs(signed_height)
        bits_per_pixel = struct.unpack("<H", data[28:30])[0]
    else:
        raise ImageMetadataError("unsupported BMP header")
    return {
        "format": "BMP", "width": width, "height": height,
        "metadata": {"BitsPerPixel": bits_per_pixel}, "creation_date": None,
    }


def _find_date(metadata: dict):
    for name in ("DateTimeOriginal", "DateTimeDigitized", "DateTime", "Creation Time", "CreationTime"):
        value = metadata.get(name)
        if value:
            return value
        for section in metadata.values():
            if isinstance(section, dict) and section.get(name):
                return section[name]
    return None


def read_metadata(path: Path) -> dict:
    data = path.read_bytes()
    if data.startswith(b"\xff\xd8"):
        return _jpeg(data)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png(data)
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return _gif(data)
    if data.startswith(b"BM"):
        return _bmp(data)
    raise ImageMetadataError("file signature does not match a supported image format")
