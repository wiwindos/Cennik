from typing import Any

from cennik.core.interfaces import EvaluationContext
from cennik.core.models import Opportunity
from cennik.engines.economics import auction_net, cheapest_material
from cennik.strategies.common import opportunity


class SimpleCraftArbitrage:
    id = "simple_craft"
    version = "0.1.0"

    def evaluate(self, context: EvaluationContext) -> list[Opportunity]:
        results = []
        for recipe in context.provider.recipes():
            output = context.prices.current.get(recipe.result_item_id) if recipe.result_item_id is not None else None
            problem = context.prices.usable(output)
            if problem:
                context.reject(self.id, "result_" + problem)
                continue
            if (not recipe.qty_min or recipe.qty_min < 1 or not recipe.qty_max
                or recipe.qty_max < recipe.qty_min or not recipe.inputs):
                context.reject(self.id, "incomplete_recipe")
                continue
            binding = context.items.get(recipe.result_item_id) if recipe.result_item_id is not None else None
            if binding and binding.binding and binding.binding.lower() in {
                "bop", "bind on pickup", "bind_on_pickup", "on_pickup", "1", "soulbound"
            }:
                context.reject(self.id, "untradeable_result")
                continue
            materials, flags, timestamps = [], [], []
            if binding is None or binding.binding is None:
                flags.append("result_tradeability_unconfirmed")
            total = 0
            valid = True
            for reagent in recipe.inputs:
                price = context.prices.current.get(reagent.item_id)
                item = context.items.get(reagent.item_id)
                auction = price.min_copper if price and not context.prices.usable(price) else None
                offer = cheapest_material(auction, item.vendor_buy_copper if item else None)
                if reagent.quantity <= 0 or offer is None:
                    context.reject(self.id, "missing_material_price_or_quantity")
                    valid = False
                    break
                unit, source = offer
                total += unit * reagent.quantity
                entry: dict[str, Any] = {"item_id": reagent.item_id, "item_name": item.name if item else None,
                         "quantity": reagent.quantity,
                         "unit_price_copper": unit, "cost_copper": unit * reagent.quantity,
                         "source": source, "item_source": item.source if item else None}
                if source == "auction":
                    assert price is not None
                    flags.extend(context.prices.flags(price))
                    timestamps.append(price.observed_at)
                    entry["observation"] = price.model_dump(mode="json")
                    if price.quantity is not None and price.quantity < reagent.quantity:
                        flags.append("insufficient_observed_material_supply")
                else:
                    flags.append("npc_stock_and_access_unconfirmed")
                materials.append(entry)
            if not valid or total <= 0:
                continue
            assert output is not None and output.min_copper is not None
            # Conservative output quantity and floor sale price, rather than an
            # optimistic median. No multi-step conversion is attempted.
            gross = output.min_copper * recipe.qty_min
            net = auction_net(gross, context.config.auction_cut_percent)
            profit = net - total
            if profit < context.config.min_profit_copper:
                context.reject(self.id, "not_profitable_after_cut")
                continue
            timestamps.append(output.observed_at)
            results.append(opportunity(context, self, output, gross, total, profit,
                "potential_buy_materials_craft_sell",
                f"Один крафт: материалы {total}; минимальный выход {recipe.qty_min}, выручка {gross}, после комиссии {net}; прибыль {profit} copper.",
                quantity=recipe.qty_min, observed_at=min(timestamps),
                identity=f"{recipe.spell_id}/{recipe.version_hash}",
                flags=flags + ["profession_skill_recipe_access_unconfirmed", "material_depth_unknown",
                               "result_sale_unconfirmed"],
                included=["materials", "auction_cut"],
                excluded=["lost_deposits", "relisting_costs", "price_slippage", "crafting_time"],
                details={"spell_id": recipe.spell_id, "version_hash": recipe.version_hash,
                    "recipe": recipe.model_dump(mode="json"), "materials": materials,
                    "gross_revenue_copper": gross, "net_revenue_copper": net,
                    "auction_cut_copper": gross - net,
                    "auction_cut_percent": str(context.config.auction_cut_percent),
                    "result_unit_price_copper": output.min_copper}))
        return results
