"""The proxy's body cap must sit above the form's file-size limit.

Caddy reads `30MB` as 30 000 000 bytes while `validate_file_size` allows
30 x 1024 x 1024 = 31 457 280, so a file between the two used to get a bare
413 from Caddy instead of the translated form message.
"""

import re
from pathlib import Path

from core.validators import MAX_UPLOAD_SIZE

CADDYFILE = Path(__file__).resolve().parents[2] / "caddy/Caddyfile"
UNITS = {
    "": 1,
    "B": 1,
    "KB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "KIB": 2**10,
    "MIB": 2**20,
    "GIB": 2**30,
}
MULTIPART_HEADROOM = 64 * 1024  # the other form fields plus multipart boundaries


def _caddy_max_body_bytes():
    match = re.search(r"max_size\s+(\d+)\s*([A-Za-z]*)", CADDYFILE.read_text())
    assert match, "caddy/Caddyfile has no request_body max_size"
    number, unit = match.groups()
    return int(number) * UNITS[unit.upper()]


def test_caddy_body_cap_leaves_room_for_the_form_limit():
    assert _caddy_max_body_bytes() >= MAX_UPLOAD_SIZE + MULTIPART_HEADROOM
