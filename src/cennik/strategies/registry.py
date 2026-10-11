from cennik.core.interfaces import Strategy
from cennik.core.models import AnalyticsConfig


class StrategyRegistry:
    def __init__(self):
        self.strategies: dict[str, Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        if not strategy.id or not strategy.version or strategy.id in self.strategies:
            raise ValueError("Strategy needs a unique id and version")
        self.strategies[strategy.id] = strategy

    def selected(self, ids: list[str]) -> list[Strategy]:
        unknown = set(ids) - self.strategies.keys()
        if unknown:
            raise ValueError("Unknown strategies: " + ", ".join(sorted(unknown)))
        return [self.strategies[key] for key in sorted(ids)]

    def normalize_config(self, config: AnalyticsConfig) -> AnalyticsConfig:
        """Optional module-owned Pydantic settings without changing core models."""
        self.selected(config.enabled_strategies)
        settings = dict(config.strategy_settings)
        for identity in settings:
            if identity not in self.strategies:
                raise ValueError("Unknown strategy settings: " + identity)
            if not getattr(self.strategies[identity], "settings_model", None):
                raise ValueError("Strategy uses shared AnalyticsConfig settings: " + identity)
        for identity, strategy in self.strategies.items():
            if identity not in config.enabled_strategies and identity not in settings:
                continue
            schema = getattr(strategy, "settings_model", None)
            if schema is not None:
                settings[identity] = schema.model_validate(settings.get(identity, {})).model_dump(mode="json")
        return config.model_copy(update={"strategy_settings": settings})
