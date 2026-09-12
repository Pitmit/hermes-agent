from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from tools.tts_text_normalize import prepare_spoken_text, strip_nonspoken_blocks


class _DummyAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, **kwargs):
        raise AssertionError("not used")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


def test_prepare_spoken_text_expands_celsius_and_weather_units():
    raw = """## Christchurch today\n\n- **Now:** about **14°C**, feels like **14°C**\n- **Wind:** 9 km/h\n- **Rain:** 1.3 mm\n- **Range:** 11\u201317°C\n"""

    spoken = prepare_spoken_text(raw)

    assert "##" not in spoken
    assert "**" not in spoken
    assert "14 degrees Celsius" in spoken
    assert "11 to 17 degrees Celsius" in spoken
    assert "9 kilometres per hour" in spoken
    assert "1.3 millimetres" in spoken
    assert "°C" not in spoken
    assert "km/h" not in spoken


def test_prepare_spoken_text_polish_edge_cases():
    # Heading folds into the next sentence as a lead-in, not a bare label.
    assert prepare_spoken_text("## Weather\nIt will be sunny") == "Weather, It will be sunny."
    # Bare degree unit (no leading number) still expands.
    assert "degrees Celsius" in prepare_spoken_text("measured in °C")
    # Trailing comma is not swallowed into the amount.
    assert "300 US dollars" in prepare_spoken_text("US$300, next")
    # Real numeric rates expand, but and/or, N/A, IDs and dates are left intact.
    assert "5 dollars per month" in prepare_spoken_text("$5/month")
    assert "and/or" in prepare_spoken_text("choose and/or option")
    assert "N/A" in prepare_spoken_text("status N/A here")
    assert "2026/06/02" in prepare_spoken_text("due 2026/06/02 ok")


def test_prepare_spoken_text_strips_media_delivery_tags():
    """MEDIA:<path> delivery directives must never reach the speech provider.

    Regression guard for Patch F (2026-08-21): the strip lives in the TTS-only
    spoken-text path (strip_nonspoken_blocks), NOT in a transform_llm_output
    hook — stripping there removed the tags before the gateway's attachment
    extractor ran and silently broke all file/image delivery.
    """
    # Absolute, home-anchored and Windows-drive paths are all stripped.
    assert "MEDIA:" not in prepare_spoken_text("Here is the report MEDIA:/home/hermes/report.pdf")
    assert "MEDIA:" not in prepare_spoken_text("Your file MEDIA:~/voice-memos/out.ogg is ready")
    assert "MEDIA:" not in strip_nonspoken_blocks("done MEDIA:C:\\Users\\p\\deck.pptx")
    assert prepare_spoken_text("Fertig MEDIA:/home/hermes/My_File.pdf danke") == "Fertig danke"
    assert prepare_spoken_text('Fertig MEDIA:"/home/hermes/My File.pdf" danke') == "Fertig danke"
    assert prepare_spoken_text("Fertig MEDIA:'/home/hermes/My File.pdf' danke") == "Fertig danke"
    # Path-anchored: quoted and bare prose words are left untouched.
    assert "MEDIA:" in prepare_spoken_text("discussing MEDIA: strategy for the campaign")
    assert 'MEDIA:"strategy"' in prepare_spoken_text('discussing MEDIA:"strategy" for the campaign')
