"""Intent classifier coverage (English, Hinglish, Devanagari, natural variations)."""

from __future__ import annotations

import pytest

from vera.conversation.intents import classify, detect_language, parse_wait

SLOTS = [{"label": "Wed 5 Nov, 6pm"}, {"label": "Thu 6 Nov, 5pm"}]


@pytest.mark.parametrize("msg,intent", [
    ("Yes", "accept"), ("yes please send it", "accept"), ("Ok lets do it. Whats next?", "accept"),
    ("go ahead", "accept"), ("haan bhej do", "accept"), ("CONFIRM", "accept"), ("sounds good, do it", "accept"),
    ("I want to join", "accept"), ("I'm interested", "accept"), ("हाँ", "accept"),
    ("Not interested", "reject"), ("no thanks", "reject"), ("nahi chahiye", "reject"), ("we don't need this", "reject"),
    ("Not now", "postpone"), ("busy, call me tomorrow", "postpone"), ("baad mein baat karte hain", "postpone"),
    ("will think and revert later", "postpone"), ("in 30 mins", "postpone"),
    ("Stop messaging me", "opt_out"), ("unsubscribe", "opt_out"), ("please don't message me again", "opt_out"),
    ("band karo ye messages", "opt_out"),
    ("this is useless, you are wasting my time", "hostile"),
    ("Thank you for contacting us! Our team will respond shortly.", "auto_reply"),
    ("Thanks for reaching out. We are currently closed and will get back to you soon.", "auto_reply"),
    ("Aapki jaankari ke liye bahut shukriya, hamari team jald hi aapse sampark karegi", "auto_reply"),
    ("can you help me with my GST filing?", "off_topic"), ("need a business loan, can you help", "off_topic"),
    ("How much does it cost?", "question"), ("what is the source?", "question"), ("kitna lagega?", "question"),
    ("thanks", "thanks"), ("👍", "thanks"),
    ("hmm", "ambiguous"), ("maybe", "ambiguous"), ("", "ambiguous"),
    ("Balayage mostly this week", "statement"),
])
def test_classify(msg, intent):
    assert classify(msg).name == intent


def test_not_interested_is_not_acceptance():
    assert classify("not interested, thanks").name == "reject"


def test_slot_choice():
    assert classify("1", slots=SLOTS).slot_index == 0
    assert classify("2", slots=SLOTS).slot_index == 1
    assert classify("Thursday works", slots=SLOTS).slot_index == 1
    assert classify("first one please", slots=SLOTS).slot_index == 0


def test_repeat_escalates_to_auto_reply():
    assert classify("We are open 9 to 9 all days", repeat_count=2).name == "auto_reply"
    assert classify("yes", repeat_count=5).name == "accept"  # short replies repeat naturally


def test_hostile_with_stop_is_opt_out_with_flag():
    i = classify("Stop messaging me. This is useless spam.")
    assert i.name == "opt_out" and "hostile" in i.flags


def test_language_detection():
    assert detect_language("haan theek hai, bhej do") == "hinglish"
    assert detect_language("नमस्ते") == "hindi"
    assert detect_language("please send it") == "en"


def test_parse_wait_durations():
    assert parse_wait("in 2 hours", None) == 7200
    assert parse_wait("give me 15 min", None) == 900
    assert parse_wait("next week", None) == 7 * 86400
    assert parse_wait("busy", None) == 1800
