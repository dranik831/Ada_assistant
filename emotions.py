import asyncio
import json

class EmotionSystem:
    def __init__(self, db=None):
        self.db = db
        self._lock = asyncio.Lock()
        self.state = {
            "happiness": 75,
            "anger": 0,
            "sadness": 5,
            "fear": 0,
            "confidence": 75,
            "energy": 80,
            "curiosity": 80,
            "loneliness": 10,
            "stress": 0
        }
        self.baseline = dict(self.state)

    async def get_state(self) -> dict:
        async with self._lock:
            if self.db:
                raw = await self.db.get_kv("emotion_state")
                if raw:
                    try:
                        saved = json.loads(raw)
                        for key in self.state:
                            if key in saved:
                                self.state[key] = max(0, min(100, int(saved[key])))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
            return dict(self.state)

    async def apply_deltas(self, deltas: dict):
        async with self._lock:
            for k, delta in deltas.items():
                if k in self.state and isinstance(delta, (int, float)):
                    self.state[k] = max(0, min(100, int(self.state[k] + delta)))
            await self._persist()

    async def set_emotion(self, emotion: str, value: int):
        async with self._lock:
            if emotion in self.state:
                self.state[emotion] = max(0, min(100, int(value)))
                await self._persist()

    async def decay_towards_baseline(self):
        async with self._lock:
            for k in self.state:
                diff = self.baseline[k] - self.state[k]
                if abs(diff) > 0:
                    step = 1 if diff > 0 else -1
                    self.state[k] += step
            await self._persist()

    async def _persist(self):
        if self.db:
            await self.db.set_kv("emotion_state", json.dumps(self.state, ensure_ascii=False))
