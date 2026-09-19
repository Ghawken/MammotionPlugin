"""App key and secret as taken from the mammotion android app.

Values below track the upstream pymammotion 0.8.14 PyPI wheel (checked 2026-09-20).
Upstream removed these from its public source and injects them at build time, so
future updates require decoding them from a released wheel, not from GitHub.
APP_KEY and APP_SECRET are a matched pair for the Aliyun IoT gateway — never
update one without the other.
"""

APP_KEY = "34219027"
APP_SECRET = "ecc533f807e5369b1b9dd5169d6fc534"
APP_VERSION = "2.3.18.21"
ALIYUN_DOMAIN = "api.link.aliyun.com"
MAMMOTION_DOMAIN = "https://id.mammotion.com"
MAMMOTION_API_DOMAIN = "https://domestic.mammotion.com"
MAMMOTION_CLIENT_ID = "MADKALUBAS"
MAMMOTION_CLIENT_SECRET = "GshzGRZJjuMUgd2sYHM7"

MAMMOTION_OUATH2_CLIENT_ID = "GxebgSt8si6pKqR"
MAMMOTION_OUATH2_CLIENT_SECRET = "JP0508SRJFa0A90ADpzLINDBxMa4Vj"
