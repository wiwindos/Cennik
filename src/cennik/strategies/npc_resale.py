from __future__ import annotations

from decimal import Decimal

from cennik.core.interfaces import EvaluationContext
from cennik.core.models import Item, Opportunity
from cennik.engines.economics import auction_net
from cennik.engines.liquidity import CraftLiquidity
from cennik.strategies.common import opportunity

UNTRADEABLE = {"bop", "bind on pickup", "bind_on_pickup", "on_pickup", "1", "soulbound",
               "binds when picked up", "quest", "quest_item", "4"}


class NpcToAuction:
    id = "npc_to_auction"
    version = "1.1.0"

    def evaluate(self, context: EvaluationContext) -> list[Opportunity]:
        results = []
        liquidity = CraftLiquidity(context)
        for item_id, price in context.prices.current.items():
            item = context.items.get(item_id)
            catalog_lookup = getattr(context.provider, "npc_catalog_reference", None)
            catalog = (catalog_lookup(item_id) if catalog_lookup else None) or {}
            # The catalogue is a fallback for this strategy only. Never make its
            # raw buy a normalised NPC material price for other craft strategies.
            use_catalog = bool(catalog.get("buy_copper") and (not item or item.vendor_buy_copper is None))
            if use_catalog:
                raw_quantity = catalog["buy_quantity"] or 1
                item = (item or Item(item_id=item_id)).model_copy(update={
                    "name": (item.name if item else None) or catalog["name"],
                    "vendor_buy_copper": (catalog["buy_copper"] + raw_quantity - 1) // raw_quantity})
            if not item or item.vendor_buy_copper is None or item.vendor_buy_copper <= 0:
                context.reject(self.id, "missing_vendor_buy_price")
                continue
            if item.binding and item.binding.strip().lower() in UNTRADEABLE:
                context.reject(self.id, "untradeable_result")
                continue
            problem = context.prices.usable(price)
            if problem:
                context.reject(self.id, "result_" + problem)
                continue
            assert price is not None and price.min_copper is not None
            lookup = getattr(context.provider, "npc_reference", None)
            reference = lookup(item_id) if lookup and item.source == "NPC captured in game" else None
            quantity, capital = 1, item.vendor_buy_copper
            if reference:
                quantity, capital = reference["batch_quantity"], reference["batch_price_copper"]
            elif use_catalog:
                quantity, capital = catalog["buy_quantity"] or 1, catalog["buy_copper"]
            source = "auctionhouse.fyi client catalog" if use_catalog else item.source
            evidence, problem = liquidity.check(price, quantity)
            if problem:
                context.reject(self.id, problem)
                continue
            gross = price.min_copper * quantity
            net = auction_net(gross, context.config.auction_cut_percent)
            profit = net - capital
            if profit < context.config.min_profit_copper:
                context.reject(self.id, "not_profitable_after_cut")
                continue
            sales = evidence["conservative_sales_per_day"]
            max_batch = (int(context.config.craft_max_batch_sales_percent * Decimal(sales) / 100)
                         if context.config.craft_liquidity_enabled and sales is not None else None)
            flags = ["npc_stock_and_access_unconfirmed", "npc_live_buy_price_unconfirmed", "result_sale_unconfirmed"]
            unit_unknown = bool(use_catalog and catalog["buy_quantity"] is None)
            if use_catalog:
                flags.append("npc_catalog_vendor_unconfirmed")
                if unit_unknown:
                    flags.append("npc_catalog_purchase_unit_unconfirmed")
                if catalog["catalog"]["requires_release_refresh"]:
                    flags.append("npc_catalog_release_refresh_required")
            if item.binding is None:
                flags.append("result_tradeability_unconfirmed")
            material = {"item_id": item_id, "item_name": item.name, "quantity": quantity,
                        "unit_price_copper": item.vendor_buy_copper, "cost_copper": capital,
                        "source": "npc", "item_source": source,
                        "npc_batch_quantity": quantity, "npc_batch_price_copper": capital}
            result = opportunity(context, self, price, gross, capital, profit,
                "potential_buy_npc_sell_auction",
                ("Условный кандидат: buy API принят за цену 1 шт.; продавец и единица цены не подтверждены. " if unit_unknown else "") +
                f"Покупка у NPC: {quantity} шт. за {capital}; ориентир продажи на AH {gross}, "
                f"после комиссии {net}; прибыль {profit} copper за партию NPC. "
                "Цену, запас торговца и передаваемость нужно проверить в игре.",
                quantity=quantity, flags=flags, included=["npc_purchase", "auction_cut"],
                excluded=["lost_deposits", "relisting_costs", "price_slippage", "travel_time"],
                details={"materials": [material], "npc_offer": {"unit_price_copper": item.vendor_buy_copper,
                    "batch_quantity": quantity, "batch_price_copper": capital,
                    "source": source, "stock": None, "access_confirmed": False,
                    "purchase_unit_confirmed": not unit_unknown,
                    "conditional_unit_assumption": unit_unknown,
                    "catalog_reference": catalog if use_catalog else None,
                    "binding": item.binding, "requires_live_confirmation": True, "captured_reference": reference},
                    "liquidity": evidence, "max_batch_by_liquidity": max_batch,
                    "gross_revenue_copper": gross, "net_revenue_copper": net,
                    "auction_cut_copper": gross - net, "auction_cut_percent": str(context.config.auction_cut_percent),
                    "result_unit_price_copper": price.min_copper,
                    "scope": "conditional one-unit API buy estimate" if unit_unknown else
                             "one NPC purchase pack; stock, access, current price and tradeability require in-game confirmation"})
            results.append(result.model_copy(update={"item_name": item.name or str(item_id)}))
        return results
