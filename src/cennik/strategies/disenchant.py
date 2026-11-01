from __future__ import annotations

from collections import defaultdict
from fractions import Fraction

from cennik.core.interfaces import EvaluationContext
from cennik.core.models import Opportunity, PriceObservation
from cennik.engines.craft_graph import CraftGraph, MaterialOffer, SearchLimit, complete
from cennik.engines.disenchant import BUNDLED, DisenchantRules, expected_gross
from cennik.engines.economics import auction_net, cheapest_material
from cennik.engines.liquidity import CraftLiquidity
from cennik.strategies.common import opportunity


class DisenchantValuation:
    def __init__(self, context: EvaluationContext):
        self.context = context
        self.rules = context.disenchant or DisenchantRules.model_validate_json(BUNDLED.read_bytes())
        self.metadata = self.rules.metadata()
        self.liquidity = CraftLiquidity(context)

    def quote(self, item_id: int, quantity: int) -> tuple[dict | None, str | None]:
        if item_id in self.rules.exclusions:
            return None, "disenchant_excluded"
        pool = self.rules.yields(item_id)
        if not pool:
            return None, "missing_disenchant_rule"
        rows, timestamps, flags = [], [], ["disenchant_expected_value", "disenchant_eligibility_unconfirmed",
                                           "disenchant_rules_pending_release_verification", "material_sale_unconfirmed"]
        for output in pool:
            price = self.context.prices.current.get(output.material_item_id)
            problem = self.context.prices.usable(price)
            if problem:
                return None, "disenchant_material_" + problem
            assert price is not None and price.min_copper is not None
            count = output.expected_count
            evidence, problem = self.liquidity.check(price, count * quantity)
            if problem:
                return None, "disenchant_material_" + problem
            fraction = Fraction(count)
            item = self.context.items.get(output.material_item_id)
            rows.append({**output.model_dump(mode="json"), "name": item.name if item else str(output.material_item_id),
                         "expected_count": str(count), "expected_quantity": str(count * quantity),
                         "expected_units_numerator": fraction.numerator,
                         "expected_units_denominator": fraction.denominator,
                         "unit_price_copper": price.min_copper,
                         "expected_gross_copper": int(count * quantity * price.min_copper),
                         "price": price.model_dump(mode="json"), "liquidity": evidence})
            timestamps.append(price.observed_at)
            flags.extend(self.context.prices.flags(price))
        definition = self.rules.items[item_id]
        gross = expected_gross(rows, quantity)
        return {"rules": self.metadata, "item_level": definition.item_level, "quality": definition.quality,
                "item_class": definition.item_class, "quantity": quantity, "yields": rows,
                "gross": gross, "net": auction_net(gross, self.context.config.auction_cut_percent),
                "observed_at": min(timestamps), "flags": sorted(set(flags))}, None


def result(context, strategy, valuation, item_id, quantity, capital, price, *, materials=None,
           steps=None, recipe=None, flags=(), timestamps=()):
    quote, problem = valuation.quote(item_id, quantity)
    if problem:
        context.reject(strategy.id, problem)
        return None
    profit = quote["net"] - capital
    if profit < context.config.min_profit_copper:
        context.reject(strategy.id, "not_profitable_after_cut")
        return None
    model = {k: v for k, v in quote.items() if k not in {"observed_at", "flags", "gross", "net"}}
    recipe_details = {"spell_id": recipe.spell_id, "version_hash": recipe.version_hash,
                      "recipe": recipe.model_dump(mode="json")} if recipe else {}
    details = {"disenchant": model, "materials": materials or [], "steps": steps or [],
               "gross_revenue_copper": quote["gross"], "net_revenue_copper": quote["net"],
               "auction_cut_copper": quote["gross"] - quote["net"],
               "auction_cut_percent": str(context.config.auction_cut_percent),
               "scope": "Expected material proceeds; not a guaranteed result of a single disenchant",
               **recipe_details}
    value = opportunity(context, strategy, price, quote["gross"], capital, profit,
        "potential_craft_disenchant_sell" if recipe else "potential_buy_disenchant_sell",
        f"{'Крафт' if recipe else 'Покупка'} {quantity} шт. под распыление: затраты {capital}; "
        f"ожидаемая выручка материалов после комиссии {quote['net']}; ожидаемая прибыль {profit} copper. "
        "Выход случаен; используется временная модель AHledger.",
        quantity=quantity, identity=f"{recipe.spell_id if recipe else ''}/{recipe.version_hash if recipe else ''}/{valuation.metadata['sha256']}",
        observed_at=min([price.observed_at, quote["observed_at"], *timestamps]),
        flags=list(flags) + quote["flags"] + ["enchanting_skill_and_access_unconfirmed"],
        included=["materials" if recipe else "purchase", "auction_cut"],
        excluded=["lost_deposits", "relisting_costs", "price_slippage", "disenchant_time", "crafting_time"],
        details=details)
    value.item_name = valuation.rules.items[item_id].name
    return value


class BuyDisenchant:
    id = "buy_disenchant"
    version = "1.0.0"

    def evaluate(self, context: EvaluationContext) -> list[Opportunity]:
        valuation = DisenchantValuation(context)
        results = []
        for item_id, price in context.prices.current.items():
            if item_id not in valuation.rules.items:
                continue
            problem = context.prices.usable(price)
            if problem:
                context.reject(self.id, problem)
                continue
            assert price.min_copper is not None
            value = result(context, self, valuation, item_id, 1, price.min_copper, price)
            if value:
                results.append(value)
        return results


class CraftDisenchant:
    id = "craft_disenchant"
    version = "1.0.0"

    def evaluate(self, context: EvaluationContext) -> list[Opportunity]:
        valuation = DisenchantValuation(context)
        recipes = context.provider.recipes()
        offers = {}
        for ingredient_id in {r.item_id for recipe in recipes for r in recipe.inputs}:
            price, item = context.prices.current.get(ingredient_id), context.items.get(ingredient_id)
            auction = price.min_copper if price and not context.prices.usable(price) else None
            quote = cheapest_material(auction, item.vendor_buy_copper if item else None)
            if quote:
                offers[ingredient_id] = MaterialOffer(ingredient_id, *quote)
        graph = CraftGraph(recipes, offers)
        results = []
        for recipe in recipes:
            item_id = recipe.result_item_id
            if item_id not in valuation.rules.items:
                continue
            if not complete(recipe):
                context.reject(self.id, "incomplete_recipe")
                continue
            assert item_id is not None and recipe.qty_min is not None
            # Check output evidence before walking the graph.
            _, problem = valuation.quote(item_id, recipe.qty_min)
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
            quantities: dict[int, int] = defaultdict(int)
            for purchase in plan.purchases:
                quantities[purchase.offer.item_id] += purchase.quantity
            materials, timestamps, flags = [], [], ["profession_skill_recipe_access_unconfirmed",
                "material_depth_unknown", "batch_plan_not_globally_optimized"]
            for reagent, quantity in sorted(quantities.items()):
                offer = offers[reagent]
                item = context.items.get(reagent)
                entry = {"item_id": reagent, "item_name": item.name if item else str(reagent),
                         "quantity": quantity, "unit_price_copper": offer.unit_price_copper,
                         "cost_copper": offer.unit_price_copper * quantity, "source": offer.source}
                if offer.source == "auction":
                    observed = context.prices.current[reagent]
                    timestamps.append(observed.observed_at)
                    flags.extend(context.prices.flags(observed))
                    if observed.quantity is None or observed.quantity < quantity:
                        flags.append("insufficient_observed_material_supply")
                else:
                    flags.append("npc_stock_and_access_unconfirmed")
                materials.append(entry)
            steps = [{"recipe": s.recipe.model_dump(mode="json"), "batches": s.batches,
                      "required_quantity": s.required_quantity, "output_quantity": s.output_quantity,
                      "surplus_quantity": s.output_quantity - s.required_quantity,
                      "inputs": [{"item_id": r.item_id, "quantity": r.quantity * s.batches}
                                 for r in s.recipe.inputs]} for s in plan.steps]
            output_at = min(context.prices.current[y.material_item_id].observed_at for y in valuation.rules.yields(item_id))
            price = PriceObservation(item_id=item_id, market=context.provider.market, observed_at=output_at,
                                     granularity="scan", source="Craft cost + AHledger disenchant model")
            value = result(context, self, valuation, item_id, recipe.qty_min, plan.cost_copper, price,
                           materials=materials, steps=steps, recipe=recipe, flags=flags, timestamps=timestamps)
            if value:
                results.append(value)
        return results
