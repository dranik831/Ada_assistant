import json

class RelationshipManager:
    @staticmethod
    def calculate_status(trust: int, affection: int, respect: int, irritation: int) -> str:
        if irritation > 75 or (trust < 20 and respect < 20):
            return "enemy"
        if irritation > 45 or trust < 35:
            return "disliked"
        if trust >= 85 and affection >= 75 and respect >= 75:
            return "trusted"
        if trust >= 70 and affection >= 65:
            return "close_friend"
        if trust >= 50 and affection >= 45:
            return "friend"
        if trust >= 30:
            return "acquaintance"
        return "unknown"

    @staticmethod
    def apply_feedback(user_row: dict, change_score: int, new_notes: list = None) -> dict:
        rel = max(0, min(100, user_row["relationship"] + change_score))
        
        # Разложение дельты по осям
        trust = max(0, min(100, user_row["trust"] + int(change_score * 0.8)))
        affection = max(0, min(100, user_row["affection"] + int(change_score * 0.9)))
        respect = max(0, min(100, user_row["respect"] + int(change_score * 0.6)))
        irritation = max(0, min(100, user_row["irritation"] - int(change_score * 0.7)))

        status = RelationshipManager.calculate_status(trust, affection, respect, irritation)
        
        existing_notes = []
        try:
            existing_notes = json.loads(user_row.get("notes", "[]"))
        except Exception:
            pass

        if new_notes:
            for n in new_notes:
                if n not in existing_notes:
                    existing_notes.append(n)

        return {
            "relationship": rel,
            "trust": trust,
            "affection": affection,
            "respect": respect,
            "irritation": irritation,
            "status": status,
            "notes": existing_notes
        }