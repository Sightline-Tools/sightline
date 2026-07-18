from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from difflib import SequenceMatcher

from .actor_constants import CHARMED_ACTOR_PREFIX, NPC_ARTICLES, YOUR_PET_PREFIX
from .config import CANONICAL_SELF


@dataclass
class ParsedEvent:
    category: str
    source_actor: str | None
    target_actor: str | None
    amount: int | None
    ability_name: str | None
    result: str | None
    parse_confidence: float
    attack_verb: str | None = None
    damage_type: str | None = None
    absorbed_amount: int | None = None
    blocked_amount: int | None = None
    resource_type: str | None = None
    effect_name: str | None = None


MELEE_VERB_FAMILY = r"(?:hit|hits|bite|bites|slash|slashes|claw|claws|stab|stabs|bash|bashes|punch|punches|kick|kicks|crush|crushes|pierce|pierces|smash|smashes|maul|mauls|gore|gores)"

POSSESSIVE_ABILITY_DAMAGE_RE = re.compile(
    r"^(.+?)'s\s+(.+?)\s+hits\s+(.+?)(?:\s+with\s+(?:your|their|his|her|its)\s+.+?)?\s+for\s+(\d+)\s+point(?:s)?\s+of\s+(?:(.+?)\s+)?damagel?(?:\s+(\d+)\s+absorbed)?(?:\s+block\s+(\d+))?$",
    re.IGNORECASE,
)
YOUR_ABILITY_DAMAGE_RE = re.compile(
    r"^your\s+(.+?)\s+hits\s+(.+?)(?:\s+with\s+(?:your|their|his|her|its)\s+.+?)?\s+for\s+(\d+)\s+point(?:s)?\s+of\s+(?:(.+?)\s+)?damagel?(?:\s+(\d+)\s+absorbed)?(?:\s+block\s+(\d+))?$",
    re.IGNORECASE,
)
YOUR_PET_MELEE_DAMAGE_RE = re.compile(
    rf"^(your\s+pet\s+.+?)\s+({MELEE_VERB_FAMILY})\s+(.+?)(?:\s+with\s+(?:your|their|his|her|its)\s+.+?)?\s+for\s+(\d+)\s+point(?:s)?\s+of\s+(?:(.+?)\s+)?damagel?(?:\s+(\d+)\s+absorbed)?(?:\s+block\s+(\d+))?$",
    re.IGNORECASE,
)
MELEE_DAMAGE_RE = re.compile(
    rf"^(.+?)\s+({MELEE_VERB_FAMILY})\s+(.+?)(?:\s+with\s+(?:your|their|his|her|its)\s+.+?)?\s+for\s+(\d+)\s+point(?:s)?\s+of\s+(?:(.+?)\s+)?damagel?(?:\s+(\d+)\s+absorbed)?(?:\s+block\s+(\d+))?$",
    re.IGNORECASE,
)
OFFHAND_MELEE_DAMAGE_RE = re.compile(
    rf"^(.+?)\s+({MELEE_VERB_FAMILY})\s+(.+?)\s+with\s+your\s+offhand\s+for\s+(\d+)\s+point(?:s)?\s+of\s+(?:(.+?)\s+)?damagel?(?:\s+(\d+)\s+absorbed)?(?:\s+block\s+(\d+))?$",
    re.IGNORECASE,
)
IMPLIED_ABILITY_HIT_RE = re.compile(
    r"^(.+?)\s+hits\s+(.+?)(?:\s+with\s+(?:your|their|his|her|its)\s+.+?)?\s+for\s+(\d+)\s+point(?:s)?\s+of\s+(?:(.+?)\s+)?damagel?(?:\s+(\d+)\s+absorbed)?(?:\s+block\s+(\d+))?$",
    re.IGNORECASE,
)
SELF_DAMAGE_FALLBACK_RE = re.compile(
    r"^(.+?)\s+(\w+)\s+(.+?)(?:\s+with\s+(?:your|their|his|her|its)\s+.+?)?\s+for\s+(\d+)\s+point(?:s)?\s+of\s+(?:(.+?)\s+)?damagel?(?:\s+(\d+)\s+absorbed)?(?:\s+block\s+(\d+))?$",
    re.IGNORECASE,
)
ATTACK_MISS_RE = re.compile(
    r"^(.+?)\s+tr(?:y|ies)\s+to\s+(\w+)\s+(.+?)\s+but\s+miss(?:es)?l?$",
    re.IGNORECASE,
)
ATTACK_AVOID_RE = re.compile(
    r"^(.+?)\s+tr(?:y|ies)\s+to\s+(\w+)\s+(.+?)\s+but\s+(.+?)\s+(dodge(?:s)?|parr(?:y|ies))l?$",
    re.IGNORECASE,
)
HEAL_WITH_ABILITY_RE = re.compile(
    r"^(.+?)'s\s+(.+?)\s+heals\s+(.+?)\s+for\s+(\d+)\s*(\w+)?$",
    re.IGNORECASE,
)
YOUR_HEAL_RE = re.compile(
    r"^your\s+(.+?)\s+heals\s+(.+?)\s+for\s+(\d+)\s*(\w+)?$",
    re.IGNORECASE,
)
HEAL_SIMPLE_RE = re.compile(r"^(.+?)\s+heals\s+(.+?)\s+for\s+(\d+)\s*(\w+)?$", re.IGNORECASE)
CAST_START_RE = re.compile(r"^(.+?)\s+begin(?:s)?\s+casting\s+(.+)$", re.IGNORECASE)
KILL_RE = re.compile(r"^you\s+have\s+slain\s+(.+)$", re.IGNORECASE)
INFO_RE = re.compile(r"^(stopped attacking|you feel .+|you go .+|your .+ fades|starting to attack)$", re.IGNORECASE)
DEATH_RE = re.compile(r"^(you have been slain|you died|you are dead)$", re.IGNORECASE)
EFFECT_FADE_RE = re.compile(r"^the\s+(.+?)\s+leaves\s+(.+)$", re.IGNORECASE)
CHARM_START_RE = re.compile(r"^(.+?)'s\s+mind\s+is\s+enthralled$", re.IGNORECASE)
CHARM_SERVITUDE_START_RE = re.compile(r"^(.+?)\s+is\s+charmed\s+into\s+servitude$", re.IGNORECASE)
CHARM_END_RE = re.compile(r"^(.+?)\s+is\s+no\s+longer\s+(enthralled|charmed)$", re.IGNORECASE)
STATUS_APPLIED_RE = re.compile(r"^(.+?)\s+is\s+(.+)$", re.IGNORECASE)
STATUS_REMOVED_RE = re.compile(r"^(.+?)\s+is\s+no\s+longer\s+(.+)$", re.IGNORECASE)
STATUS_BREAK_RE = re.compile(r"^(.+?)'s\s+(.+?)\s+breaks$", re.IGNORECASE)
STATUS_LOOKS_RE = re.compile(r"^(.+?)\s+looks\s+(.+)$", re.IGNORECASE)
STATUS_STAGGER_RE = re.compile(r"^(.+?)\s+staggers$", re.IGNORECASE)


def normalize_text(text: str) -> str:
    without_timestamp = re.sub(r"^\s*\[?\d{1,2}:\d{2}:\d{2}\]?\s*", "", text)
    lowered = without_timestamp.lower().replace("|", " ")
    normalized_quotes = lowered.replace("â€™", "'").replace("’", "'").replace("‘", "'").replace("`", "'")
    normalized_quotes = re.sub(r"'{2,}", "'", normalized_quotes)
    normalized_quotes = re.sub(r"^'(?=your?\b)", "", normalized_quotes)
    stripped_punct = re.sub(r"[^\w\s']", " ", normalized_quotes)
    return re.sub(r"\s+", " ", stripped_punct).strip()


def canonicalize_actor(name: str, known: set[str]) -> str:
    stripped = name.strip()
    pet_actor = _canonicalize_pet_actor(stripped)
    if pet_actor:
        return pet_actor
    lower = stripped.lower()
    if lower in {"you", "your"}:
        return CANONICAL_SELF
    if stripped in known:
        return stripped

    best_score = 0
    best_actor: str | None = None
    for actor in known:
        score = SequenceMatcher(None, stripped.lower(), actor.lower()).ratio() * 100
        if score > best_score:
            best_score = score
            best_actor = actor

    if best_actor and best_score >= 92:
        return best_actor
    return stripped


def _canonicalize_pet_actor(name: str) -> str | None:
    lower = name.lower()
    if not lower.startswith(YOUR_PET_PREFIX):
        return None

    pet_name = name[len(YOUR_PET_PREFIX) :].strip()
    if not pet_name:
        return name

    if pet_name.casefold().split(maxsplit=1)[0] in NPC_ARTICLES:
        return f"{CHARMED_ACTOR_PREFIX}{pet_name}"

    return f"{YOUR_PET_PREFIX}{pet_name}"


def _title(value: str | None) -> str | None:
    if value is None:
        return None
    titled = value.strip().title()
    return re.sub(r"'([A-Z])", lambda match: f"'{match.group(1).lower()}", titled)


SELF_REFERENTIAL_TARGETS = {"them", "themself", "themselves", "himself", "herself", "itself"}


def _canonicalize_heal_target(target: str, source_actor: str, known_actors: set[str]) -> str:
    if target.strip().lower() in SELF_REFERENTIAL_TARGETS:
        return source_actor
    return canonicalize_actor(target, known_actors)




def _parse_implied_possessive_ability(source_phrase: str, known_actors: set[str]) -> tuple[str, str] | None:
    phrase = source_phrase.strip()
    if not phrase:
        return None

    if phrase.lower().startswith("your ") and len(phrase.split()) > 1:
        return CANONICAL_SELF, phrase[5:].strip()

    known_sorted = sorted((actor for actor in known_actors if actor.lower() != "you"), key=len, reverse=True)
    lower_phrase = phrase.lower()
    for actor in known_sorted:
        actor_lower = actor.lower()
        if lower_phrase == actor_lower:
            continue
        if lower_phrase.startswith(actor_lower + " "):
            ability = phrase[len(actor):].strip()
            if ability:
                return actor, ability

    return None


def _is_probable_direct_actor_phrase(source_phrase: str, known_actors: set[str], target_phrase: str | None = None) -> bool:
    phrase = source_phrase.strip()
    if not phrase:
        return False

    lower_phrase = phrase.lower()
    if lower_phrase in {"you", "your"}:
        return True
    if any(lower_phrase == actor.lower() for actor in known_actors):
        return True

    if target_phrase and target_phrase.strip().lower() in {"you", "your"}:
        return True

    tokens = lower_phrase.split()
    if len(tokens) <= 2:
        return True
    if tokens[0] in {"a", "an", "the"}:
        return True

    return False


def _damage_category(source: str, target: str) -> str:
    if source == CANONICAL_SELF:
        return "damage_out"
    if target == CANONICAL_SELF:
        return "damage_in"
    return "damage"


def _miss_category(source: str, target: str) -> str:
    if source == CANONICAL_SELF:
        return "miss_out"
    if target == CANONICAL_SELF:
        return "miss_in"
    return "miss"


def parse_line(raw_text: str, known_actors: set[str]) -> ParsedEvent | None:
    text = normalize_text(raw_text)

    if m := POSSESSIVE_ABILITY_DAMAGE_RE.match(text):
        owner, ability, target, amount, damage_type, absorbed, blocked = m.groups()
        source_actor = canonicalize_actor(owner, known_actors)
        target_actor = canonicalize_actor(target, known_actors)
        return ParsedEvent(
            _damage_category(source_actor, target_actor),
            source_actor,
            target_actor,
            int(amount),
            _title(ability),
            "hit",
            0.98,
            attack_verb="hits",
            damage_type=_title(damage_type) if damage_type else "Physical",
            absorbed_amount=int(absorbed) if absorbed else None,
            blocked_amount=int(blocked) if blocked else None,
        )

    if m := YOUR_PET_MELEE_DAMAGE_RE.match(text):
        source, attack_verb, target, amount, damage_type, absorbed, blocked = m.groups()
        source_actor = canonicalize_actor(source, known_actors)
        target_actor = canonicalize_actor(target, known_actors)
        return ParsedEvent(
            _damage_category(source_actor, target_actor),
            source_actor,
            target_actor,
            int(amount),
            None,
            "hit",
            0.97,
            attack_verb=attack_verb.lower(),
            damage_type=_title(damage_type) if damage_type else "Physical",
            absorbed_amount=int(absorbed) if absorbed else None,
            blocked_amount=int(blocked) if blocked else None,
        )

    if m := YOUR_ABILITY_DAMAGE_RE.match(text):
        ability, target, amount, damage_type, absorbed, blocked = m.groups()
        source_actor = CANONICAL_SELF
        target_actor = canonicalize_actor(target, known_actors)
        return ParsedEvent(
            _damage_category(source_actor, target_actor),
            source_actor,
            target_actor,
            int(amount),
            _title(ability),
            "hit",
            0.98,
            attack_verb="hits",
            damage_type=_title(damage_type) if damage_type else "Physical",
            absorbed_amount=int(absorbed) if absorbed else None,
            blocked_amount=int(blocked) if blocked else None,
        )

    if m := IMPLIED_ABILITY_HIT_RE.match(text):
        source_phrase, target, amount, damage_type, absorbed, blocked = m.groups()
        implied = _parse_implied_possessive_ability(source_phrase, known_actors)
        if implied is not None:
            implied_source, implied_ability = implied
            source_actor = canonicalize_actor(implied_source, known_actors)
            target_actor = canonicalize_actor(target, known_actors)
            return ParsedEvent(
                _damage_category(source_actor, target_actor),
                source_actor,
                target_actor,
                int(amount),
                _title(implied_ability),
                "hit",
                0.92,
                attack_verb="hits",
                damage_type=_title(damage_type) if damage_type else "Physical",
                absorbed_amount=int(absorbed) if absorbed else None,
                blocked_amount=int(blocked) if blocked else None,
            )

        if not _is_probable_direct_actor_phrase(source_phrase, known_actors, target):
            return None

    if m := OFFHAND_MELEE_DAMAGE_RE.match(text):
        source, _, target, amount, damage_type, absorbed, blocked = m.groups()
        source_actor = canonicalize_actor(source, known_actors)
        target_actor = canonicalize_actor(target, known_actors)
        return ParsedEvent(
            _damage_category(source_actor, target_actor),
            source_actor,
            target_actor,
            int(amount),
            "Offhand",
            "hit",
            0.96,
            attack_verb="offhand",
            damage_type=_title(damage_type) if damage_type else "Physical",
            absorbed_amount=int(absorbed) if absorbed else None,
            blocked_amount=int(blocked) if blocked else None,
        )

    if m := MELEE_DAMAGE_RE.match(text):
        source, attack_verb, target, amount, damage_type, absorbed, blocked = m.groups()
        if attack_verb.lower() == "hits" and not _is_probable_direct_actor_phrase(source, known_actors, target):
            return None
        source_actor = canonicalize_actor(source, known_actors)
        target_actor = canonicalize_actor(target, known_actors)
        return ParsedEvent(
            _damage_category(source_actor, target_actor),
            source_actor,
            target_actor,
            int(amount),
            None,
            "hit",
            0.95,
            attack_verb=attack_verb.lower(),
            damage_type=_title(damage_type) if damage_type else "Physical",
            absorbed_amount=int(absorbed) if absorbed else None,
            blocked_amount=int(blocked) if blocked else None,
        )

    if m := SELF_DAMAGE_FALLBACK_RE.match(text):
        source, attack_verb, target, amount, damage_type, absorbed, blocked = m.groups()
        source_actor = canonicalize_actor(source, known_actors)
        target_actor = canonicalize_actor(target, known_actors)
        if CANONICAL_SELF not in {source_actor, target_actor}:
            return None
        return ParsedEvent(
            _damage_category(source_actor, target_actor),
            source_actor,
            target_actor,
            int(amount),
            None,
            "hit",
            0.9,
            attack_verb=attack_verb.lower(),
            damage_type=_title(damage_type) if damage_type else "Physical",
            absorbed_amount=int(absorbed) if absorbed else None,
            blocked_amount=int(blocked) if blocked else None,
        )

    if m := ATTACK_MISS_RE.match(text):
        source, attack_verb, target = m.groups()
        source_actor = canonicalize_actor(source, known_actors)
        target_actor = canonicalize_actor(target, known_actors)
        return ParsedEvent(
            _miss_category(source_actor, target_actor),
            source_actor,
            target_actor,
            None,
            None,
            "miss",
            0.95,
            attack_verb=attack_verb.lower(),
        )

    if m := ATTACK_AVOID_RE.match(text):
        source, attack_verb, attempted_target, defended_target, result = m.groups()
        source_actor = canonicalize_actor(source, known_actors)
        target_actor = canonicalize_actor(defended_target or attempted_target, known_actors)
        return ParsedEvent(
            _miss_category(source_actor, target_actor),
            source_actor,
            target_actor,
            None,
            None,
            "parries" if result.lower() == "parry" else result.lower(),
            0.93,
            attack_verb=attack_verb.lower(),
        )

    if m := YOUR_HEAL_RE.match(text):
        ability, target, amount, resource = m.groups()
        source_actor = CANONICAL_SELF
        target_actor = _canonicalize_heal_target(target, source_actor, known_actors)
        return ParsedEvent(
            "heal",
            source_actor,
            target_actor,
            int(amount),
            _title(ability),
            "heal",
            0.96,
            resource_type=_title(resource),
        )

    if m := HEAL_WITH_ABILITY_RE.match(text):
        owner, ability, target, amount, resource = m.groups()
        source_actor = canonicalize_actor(owner, known_actors)
        target_actor = _canonicalize_heal_target(target, source_actor, known_actors)
        return ParsedEvent(
            "heal",
            source_actor,
            target_actor,
            int(amount),
            _title(ability),
            "heal",
            0.96,
            resource_type=_title(resource),
        )

    if m := HEAL_SIMPLE_RE.match(text):
        source, target, amount, resource = m.groups()
        implied = _parse_implied_possessive_ability(source, known_actors)
        source_actor: str
        ability_name: str | None = None
        direct_source = canonicalize_actor(source, known_actors)
        source_is_known_actor = any(source.lower() == actor.lower() for actor in known_actors)
        implied_source_actor = canonicalize_actor(implied[0], known_actors) if implied is not None else None
        if implied is not None and (
            not _is_probable_direct_actor_phrase(source, known_actors, target)
            or (implied_source_actor != direct_source and not source_is_known_actor)
        ):
            implied_source, implied_ability = implied
            source_actor = canonicalize_actor(implied_source, known_actors)
            ability_name = _title(implied_ability)
        else:
            source_actor = direct_source
        target_actor = _canonicalize_heal_target(target, source_actor, known_actors)
        return ParsedEvent(
            "heal",
            source_actor,
            target_actor,
            int(amount),
            ability_name,
            "heal",
            0.9,
            resource_type=_title(resource),
        )

    if m := CAST_START_RE.match(text):
        source, ability = m.groups()
        return ParsedEvent(
            "cast_start",
            canonicalize_actor(source, known_actors),
            None,
            None,
            _title(ability),
            "begin",
            0.94,
        )

    if m := KILL_RE.match(text):
        target = m.group(1)
        return ParsedEvent("kill", CANONICAL_SELF, canonicalize_actor(target, known_actors), None, None, "slain", 0.98)

    if DEATH_RE.match(text):
        return ParsedEvent("death_self", CANONICAL_SELF, CANONICAL_SELF, None, None, "dead", 0.99)

    if m := CHARM_START_RE.match(text):
        actor = m.group(1)
        return ParsedEvent(
            "charm_start",
            None,
            canonicalize_actor(actor, known_actors),
            None,
            None,
            "enthralled",
            0.98,
            effect_name="enthralled",
        )

    if m := CHARM_SERVITUDE_START_RE.match(text):
        actor = m.group(1)
        return ParsedEvent(
            "charm_start",
            None,
            canonicalize_actor(actor, known_actors),
            None,
            None,
            "charmed into servitude",
            0.98,
            effect_name="charmed into servitude",
        )

    if m := CHARM_END_RE.match(text):
        actor, effect = m.groups()
        return ParsedEvent(
            "charm_end",
            None,
            canonicalize_actor(actor, known_actors),
            None,
            None,
            f"no longer {effect.strip()}",
            0.98,
            effect_name=effect.strip(),
        )

    if m := STATUS_REMOVED_RE.match(text):
        actor, effect = m.groups()
        return ParsedEvent(
            "status_removed",
            None,
            canonicalize_actor(actor, known_actors),
            None,
            None,
            None,
            0.9,
            effect_name=effect.strip(),
        )

    if m := EFFECT_FADE_RE.match(text):
        effect, actor = m.groups()
        return ParsedEvent(
            "effect_fade",
            None,
            canonicalize_actor(actor, known_actors),
            None,
            None,
            None,
            0.9,
            effect_name=effect.strip(),
        )

    if m := STATUS_BREAK_RE.match(text):
        actor, effect = m.groups()
        return ParsedEvent(
            "status_applied",
            None,
            canonicalize_actor(actor, known_actors),
            None,
            None,
            None,
            0.88,
            effect_name=f"{effect.strip()} breaks",
        )

    if m := STATUS_LOOKS_RE.match(text):
        actor, effect = m.groups()
        return ParsedEvent(
            "status_applied",
            None,
            canonicalize_actor(actor, known_actors),
            None,
            None,
            None,
            0.88,
            effect_name=effect.strip(),
        )

    if m := STATUS_STAGGER_RE.match(text):
        actor = m.group(1)
        return ParsedEvent(
            "status_applied",
            None,
            canonicalize_actor(actor, known_actors),
            None,
            None,
            None,
            0.88,
            effect_name="staggered",
        )

    if m := STATUS_APPLIED_RE.match(text):
        actor, effect = m.groups()
        return ParsedEvent(
            "status_applied",
            None,
            canonicalize_actor(actor, known_actors),
            None,
            None,
            None,
            0.85,
            effect_name=effect.strip(),
        )

    if INFO_RE.match(text):
        return ParsedEvent("info", None, None, None, None, None, 0.9)

    return None
