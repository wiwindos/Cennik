from cennik.core.interfaces import EvaluationContext
from cennik.core.models import Opportunity
from cennik.strategies.common import opportunity


class VendorArbitrage:
    id = "vendor_arbitrage"
    version = "0.1.0"

    def evaluate(self, context: EvaluationContext) -> list[Opportunity]:
        result = []
        for item_id, price in context.prices.current.items():
            problem = context.prices.usable(price)
            item = context.items.get(item_id)
            if problem:
                context.reject(self.id, problem)
                continue
            if not item or item.vendor_sell_copper is None or item.vendor_sell_copper <= 0:
                context.reject(self.id, "missing_vendor_sell_price")
                continue
            assert price.min_copper is not None
            profit = item.vendor_sell_copper - price.min_copper
            if profit < context.config.min_profit_copper:
                context.reject(self.id, "not_profitable")
                continue
            result.append(opportunity(context, self, price, item.vendor_sell_copper,
                price.min_copper, profit, "buy_then_vendor",
                f"NPC {item.vendor_sell_copper} − наблюдаемый минимум {price.min_copper} = {profit} copper за единицу; комиссия AH не применяется.",
                included=["purchase"], details={"vendor_sell_copper": item.vendor_sell_copper,
                "profit_per_unit_copper": profit, "auction_cut_copper": 0}, confidence="moderate"))
        return result
