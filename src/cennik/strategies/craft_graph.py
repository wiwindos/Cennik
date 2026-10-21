from collections import defaultdict
from typing import Any

from pydantic import Field

from cennik.core.interfaces import EvaluationContext
from cennik.core.models import Model, Opportunity
from cennik.engines.craft_graph import CraftGraph, MaterialOffer, SearchLimit, complete
from cennik.engines.economics import auction_net, cheapest_material
from cennik.engines.liquidity import CraftLiquidity
from cennik.strategies.common import opportunity


class CraftGraphSettings(Model):
    max_depth: int = Field(default=5, ge=2, le=10)
    max_nodes: int = Field(default=2000, ge=1, le=20000)
    max_crafts: int = Field(default=10000, ge=1, le=100000)


class CraftGraphArbitrage:
    id = "craft_graph"
    version = "0.3.0"
    settings_model = CraftGraphSettings

    def evaluate(self, context: EvaluationContext) -> list[Opportunity]:
        settings = self.settings_model.model_validate(context.config.strategy_settings.get(self.id, {}))
        recipes = context.provider.recipes()
        offers = {}
        for item_id in {r.item_id for recipe in recipes for r in recipe.inputs}:
            price, item = context.prices.current.get(item_id), context.items.get(item_id)
            auction = price.min_copper if price and not context.prices.usable(price) else None
            quote = cheapest_material(auction, item.vendor_buy_copper if item else None)
            if quote:
                offers[item_id] = MaterialOffer(item_id, *quote)
        graph = CraftGraph(recipes, offers, **settings.model_dump())
        results = []
        liquidity = CraftLiquidity(context)
        for recipe in recipes:
            if not complete(recipe):
                context.reject(self.id, "incomplete_recipe")
                continue
            output = context.prices.current.get(recipe.result_item_id) if recipe.result_item_id is not None else None
            problem = context.prices.usable(output)
            if problem:
                context.reject(self.id, "result_" + problem)
                continue
            item = context.items.get(recipe.result_item_id) if recipe.result_item_id is not None else None
            if item and item.binding and item.binding.lower() in {
                "bop", "bind on pickup", "bind_on_pickup", "on_pickup", "1", "soulbound"
            }:
                context.reject(self.id, "untradeable_result")
                continue
            assert output is not None and recipe.qty_min is not None
            evidence, problem = liquidity.check(output, recipe.qty_min)
            if problem:
                context.reject(self.id, problem)
                continue
            try:
                plan = graph.plan(recipe)
            except SearchLimit:
                context.reject(self.id, "craft_graph_search_limit")
                continue
            if plan is None or plan.cost_copper <= 0:
                context.reject(self.id, "no_complete_craft_path")
                continue
            if len(plan.steps) < 2:
                context.reject(self.id, "single_step_or_buy_is_cheaper")
                continue
            assert output is not None and output.min_copper is not None and recipe.qty_min is not None
            gross = output.min_copper * recipe.qty_min
            net = auction_net(gross, context.config.auction_cut_percent)
            profit = net - plan.cost_copper
            if profit < context.config.min_profit_copper:
                context.reject(self.id, "not_profitable_after_cut")
                continue
            flags = ["profession_skill_recipe_access_unconfirmed", "material_depth_unknown",
                     "result_sale_unconfirmed", "cooldown_and_conversion_access_unconfirmed",
                     "batch_plan_not_globally_optimized"]
            if item is None or item.binding is None:
                flags.append("result_tradeability_unconfirmed")
            timestamps = [output.observed_at]
            quantities: dict[int, int] = defaultdict(int)
            for purchase in plan.purchases:
                quantities[purchase.offer.item_id] += purchase.quantity
            materials = []
            for item_id, quantity in sorted(quantities.items()):
                offer, item = offers[item_id], context.items.get(item_id)
                entry: dict[str, Any] = {"item_id": item_id, "item_name": item.name if item else None,
                    "quantity": quantity, "unit_price_copper": offer.unit_price_copper,
                    "cost_copper": offer.unit_price_copper * quantity, "source": offer.source,
                    "item_source": item.source if item else None}
                if offer.source == "auction":
                    price = context.prices.current[item_id]
                    timestamps.append(price.observed_at)
                    flags.extend(context.prices.flags(price))
                    entry["observation"] = price.model_dump(mode="json")
                    if price.quantity is not None and price.quantity < quantity:
                        flags.append("insufficient_observed_material_supply")
                else:
                    flags.append("npc_stock_and_access_unconfirmed")
                materials.append(entry)
            steps = [{"recipe": step.recipe.model_dump(mode="json"), "batches": step.batches,
                      "required_quantity": step.required_quantity, "output_quantity": step.output_quantity,
                      "surplus_quantity": step.output_quantity - step.required_quantity,
                      "inputs": [{"item_id": r.item_id, "quantity": r.quantity * step.batches}
                                 for r in step.recipe.inputs]} for step in plan.steps]
            results.append(opportunity(context, self, output, gross, plan.cost_copper, profit,
                "potential_buy_materials_multistep_craft_sell",
                f"Цепочка из {len(steps)} этапов: закупка {plan.cost_copper}; выход {recipe.qty_min}, "
                f"выручка {gross}, после комиссии {net}; прибыль {profit} copper. Излишки не оценены в прибыль.",
                quantity=recipe.qty_min, observed_at=min(timestamps),
                identity=f"{recipe.spell_id}/{recipe.version_hash}", flags=flags,
                included=["materials", "auction_cut"],
                excluded=["lost_deposits", "relisting_costs", "price_slippage", "crafting_time", "cooldown_time"],
                details={"spell_id": recipe.spell_id, "version_hash": recipe.version_hash,
                    "recipe": recipe.model_dump(mode="json"), "materials": materials, "steps": steps,
                    "gross_revenue_copper": gross, "net_revenue_copper": net,
                    "auction_cut_copper": gross - net, "auction_cut_percent": str(context.config.auction_cut_percent),
                    "result_unit_price_copper": output.min_copper,
                    "scope": "one final craft; intermediate crafts use integer batches; surplus has no credited value",
                    "search": {**settings.model_dump(), "nodes": graph.nodes, "cycles_blocked": graph.cycles_blocked},
                    "surplus_value_copper": 0, "liquidity": evidence}))
        return results
