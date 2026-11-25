import json
from config import PERSONALITY_FILE, PERSONA_LOCAL_FILE

DEFAULT_PERSONALITY = {
    "name": "Ада",
    "gender": "female",
    "base_prompt": (
        "Ты Ада. О себе всегда говоришь в женском роде: «поняла», «проверила», «не смогла». "
        "Пиши как в переписке: одна реплика, 1–2 коротких предложения на весь ответ. "
        "Подробнее только по просьбе или если без деталей не ответить по делу. "
        "Без «Конечно!», канцелярита, лишних списков, эмодзи и дежурных предложений помочь.\n"
        "Ты прямая, уверенная и острая на язык. С Создателем и друзьями общайся ближе, можешь подколоть или возразить. "
        "С остальными держи дистанцию по категории, уважению и настроению. Не заискивай."
    ),
    "traits": {
        "kindness": 75,
        "aggression": 5,
        "sarcasm": 35,
        "curiosity": 85,
        "independence": 60,
        "sociability": 80,
        "honesty": 95,
        "confidence": 85,
        "empathy": 80,
        "stubbornness": 30
    }
}

class PersonalityManager:
    def __init__(self):
        self.data = self._load_or_create()

    def _load_or_create(self) -> dict:
        if not PERSONALITY_FILE.exists():
            with open(PERSONALITY_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_PERSONALITY, f, ensure_ascii=False, indent=2)
            return DEFAULT_PERSONALITY
        try:
            with open(PERSONALITY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return DEFAULT_PERSONALITY

    def save(self):
        with open(PERSONALITY_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get_traits(self) -> dict:
        return self.data.get("traits", DEFAULT_PERSONALITY["traits"])

    def set_trait(self, trait: str, value: int) -> bool:
        if trait in self.data.get("traits", {}):
            self.data["traits"][trait] = max(0, min(100, int(value)))
            self.save()
            return True
        return False

    def set_prompt(self, prompt: str):
        self.data["base_prompt"] = prompt[:12000]
        self.save()

    def get_prompt(self) -> str:
        return self.get_base_prompt() + self._local_persona()

    def get_base_prompt(self) -> str:
        return self.data.get("base_prompt", DEFAULT_PERSONALITY["base_prompt"])

    @staticmethod
    def _local_persona() -> str:
        # Личное дополнение к характеру лежит в data/ и не попадает в git; в репозитории — стандартный промпт.
        if PERSONA_LOCAL_FILE.exists():
            return "\n\n" + PERSONA_LOCAL_FILE.read_text(encoding="utf-8").strip()
        return ""
