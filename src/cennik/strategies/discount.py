from dataclasses import asdict
from decimal import Decimal

from cennik.core.interfaces import EvaluationContext
from cennik.core.models import Opportunity
from cennik.engines.economics import auction_net
from cennik.strategies.common import opportunity


class HistoricalDiscount:
    id = "historical_discount"
    version = "0.1.0"

    def evaluate(self, context: EvaluationContext) -> list[Opportunity]:
        result = []
        for item_id, price in context.prices.current.items():
            problem = context.prices.usable(price)
            if problem:
                context.reject(self.id, problem)
                continue
            reference = context.prices.reference(item_id)
            if reference.flags or not reference.copper:
                for flag in reference.flags or ["missing_history"]:
                    context.reject(self.id, flag)
                continue
            assert price.min_copper is not None
            discount = (Decimal(reference.copper - price.min_copper) * 100 / reference.copper)
            if discount < context.config.discount_percent:
                context.reject(self.id, "discount_below_threshold")
                continue
            net = auction_net(reference.copper, context.config.auction_cut_percent)
            profit = net - price.min_copper
            if profit < context.config.min_profit_copper:
                context.reject(self.id, "not_profitable_after_cut")
                continue
            result.append(opportunity(context, self, price, reference.copper, price.min_copper,
                profit, "potential_buy_then_resell",
                f"Историческая медиана {reference.copper}; скидка {discount:.2f}%. При продаже по ориентиру после комиссии прибыль {profit} copper; продажа не подтверждена.",
                flags=["historical_resale_price_unconfirmed"], included=["purchase", "auction_cut"],
                excluded=["lost_deposits", "relisting_costs", "price_slippage"],
                details={"history": asdict(reference), "discount_percent": str(discount),
                    "net_reference_copper": net, "auction_cut_copper": reference.copper - net,
                    "auction_cut_percent": str(context.config.auction_cut_percent),
                    "sales_note": "source estimate, not confirmed sales or a sale probability"},
                confidence="moderate"))
        return result
