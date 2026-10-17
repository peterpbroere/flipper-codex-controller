"""PC-side session naming; names never change lifecycle or alert identity."""
import unicodedata


def resolve_session(deck, selector):
    rows = deck.view(limit=None)
    matches = [row for row in rows if row["session_id"] == selector or
               (len(selector) >= 4 and row["id"].startswith(selector.lower()))]
    if len(matches) != 1:
        raise ValueError("Session ID is ambiguous or unknown; run `agent-deck list` and use its ID")
    return matches[0]


def rename_session(store, deck, selector, name=None):
    row = resolve_session(deck, selector)
    if name is not None:
        name = name.strip()
        if not 1 <= len(name) <= 32 or any(unicodedata.category(c).startswith("C") for c in name):
            raise ValueError("Use a name of 1-32 characters without control characters")
    store.append({"session_id": row["session_id"], "kind": "SessionName", "name": name or ""})
    return row
