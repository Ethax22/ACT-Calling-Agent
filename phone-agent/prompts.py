"""Fixed phone wording. Changing GREETING needs a new pre-rendered clip (greeting_cache)."""
import os
import re
from dataclasses import dataclass

GREETING = (
    "Welcome to Agni College of Technology, this is Agni AI. "
    "This call may be recorded to help us serve you better. How can I help you today?"
)


# ---------------------------------------------------------------- outbound (a lead called back)
# From the Indus outbound agent (docs/Indus_Outbound_Agent_Capture.md), with the same defaults
# for a campaign row that has no value.
@dataclass
class Lead:
    name: str = ""
    course: str = ""
    source: str = ""          # where they enquired: "our website", "a phone call", ...
    date: str = ""            # when they enquired, as written in the campaign ("2026-10-04", "recently")
    phone: str = ""

    def spoken_name(self) -> str:
        return self.name.strip() or "there"

    def spoken_course(self) -> str:
        return self.course.strip() or "engineering"


def admissions_phone() -> str:
    return re.sub(r"\D", "", os.environ.get("ADMISSIONS_PHONE", ""))[-10:] or "7358011190"


def outbound_greeting(lead: Lead) -> str:
    return (f"Hello {lead.spoken_name()}, this is Agni AI calling from Agni College of Technology. "
            f"This call may be recorded. You recently enquired about {lead.spoken_course()}. "
            "Is this a good time to talk for a minute?")


def voicemail_message(language: str = "en") -> str:
    """Said once when the call reaches voicemail, then the call ends. The number is written as
    single digits so it is read digit by digit (Calling_Agent_Outbound_Indus_Changes.md)."""
    digits = " ".join(admissions_phone())
    if language == "ta":
        return (f"வணக்கம், நான் Agni College of Technology-லிருந்து Agni AI. உங்க admission enquiry பத்தி "
                f"பேச அழைச்சோம். மறுபடியும் அழைக்கிறோம். Admissions-ஐ {digits} என்ற எண்ணில் தொடர்பு கொள்ளலாம். நன்றி.")
    return ("Hello, this is Agni AI from Agni College of Technology, following up on your admissions enquiry. "
            f"We will try again later. You can also reach admissions on {digits}. Thank you.")
