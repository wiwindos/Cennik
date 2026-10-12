from cennik.strategies.craft import SimpleCraftArbitrage
from cennik.strategies.craft_graph import CraftGraphArbitrage
from cennik.strategies.discount import HistoricalDiscount
from cennik.strategies.registry import StrategyRegistry
from cennik.strategies.vendor import VendorArbitrage


def default_registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    for strategy in (VendorArbitrage(), HistoricalDiscount(), SimpleCraftArbitrage(), CraftGraphArbitrage()):
        registry.register(strategy)
    return registry
