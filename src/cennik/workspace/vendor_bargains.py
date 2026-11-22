"""One market-floor quote per item, compared with its NPC sell value."""
from datetime import UTC, datetime
from decimal import Decimal

import pyarrow as pa

from .neutral import variant_key


class VendorBargains:
    def __init__(self, workspace):
        self.workspace = workspace
        self.references = None

    def reference_rows(self):
        w = self.workspace
        generation = w.generation()
        if self.references and self.references[0] == generation:
            return self.references[1]
        with w.provider(w.market.path, w.market_key, w.evaluation_now()) as provider:
            items = provider.items()
        rows = []
        for row in w.market.rows("SELECT DISTINCT item_id,variant FROM working_offers WHERE vendor_sell IS NULL"):
            item = items.get(row['item_id'])
            # Suffix/bonus variants need their own observed vendor value.
            if item and item.vendor_sell_copper is not None and variant_key(row['item_id'], row['variant']) == str(row['item_id']):
                rows.append({**row, 'vendor_sell': item.vendor_sell_copper, 'name': item.name,
                    'source': 'Ручная цена' if item.source == 'manual' else 'Справочник NPC / предметов',
                    'phase': item.source_phase})
        table = pa.Table.from_pylist(rows, schema=pa.schema([
            ('item_id', pa.int64()), ('variant', pa.string()), ('vendor_sell', pa.int64()),
            ('name', pa.string()), ('source', pa.string()), ('phase', pa.string())]))
        self.references = generation, table
        return table

    def report(self, *, q='', sort='closeness', order='asc', min_markup=None, max_markup=None,
               max_age_seconds=0, offset=0, limit=50,stackable_only=False):
        w, m = self.workspace, self.workspace.market
        columns = {'closeness':'distance_percent','markup':'markup_percent','price':'min_unit_copper',
            'vendor':'vendor_sell_copper','quantity':'available_quantity','name':'name'}
        if sort not in columns or order not in {'asc', 'desc'}:
            raise ValueError('Неизвестная сортировка')
        if min_markup is not None and max_markup is not None and Decimal(str(min_markup))>Decimal(str(max_markup)):
            raise ValueError('Наценка: значение «от» больше значения «до»')
        now = datetime.now(UTC)
        translated = m.names.search(q, 30000) if q else []
        with m.lock:
            refs = self.reference_rows()
            head = m.rows("SELECT * FROM scan_registry WHERE complete AND market=? ORDER BY observed_at DESC,received_at DESC LIMIT 1", [w.market_key])
            cursor = m.db.cursor()
            cursor.register('vendor_bargain_references', refs)
            def rows(query, args):
                result = cursor.execute(query, args)
                names = [column[0] for column in result.description]
                return [dict(zip(names, row, strict=True)) for row in result.fetchall()]
            try:
                sql = """WITH stack_facts AS (
                    SELECT item_id,bool_or(quantity>1 OR divisible IS TRUE) observed_stackable
                    FROM working_offers WHERE quantity>0 GROUP BY item_id
                ), priced AS (
                    SELECT o.scan_id,o.sequence,o.item_id,o.variant,coalesce(o.name,r.name,'Предмет') AS name,
                        o.quantity,o.buyout buyout_copper,o.observed_at,o.owned,
                        CASE WHEN s.observed_stackable OR i.stack_size>1 THEN true
                             WHEN i.stack_size=1 THEN false ELSE NULL END stackable,
                        coalesce(o.vendor_sell,r.vendor_sell) vendor_sell_copper,
                        CASE WHEN o.vendor_sell IS NOT NULL THEN 'Свой скан · точный вариант' ELSE r.source END vendor_source,
                        CASE WHEN o.vendor_sell IS NOT NULL THEN NULL ELSE r.phase END vendor_phase
                    FROM working_offers o LEFT JOIN vendor_bargain_references r
                        ON o.item_id=r.item_id AND o.variant IS NOT DISTINCT FROM r.variant
                    LEFT JOIN stack_facts s ON s.item_id=o.item_id
                    LEFT JOIN items i ON i.item_id=o.item_id
                    WHERE o.buyout>0 AND o.quantity>0
                ), book AS (
                    SELECT *,sum(quantity) OVER(PARTITION BY item_id) available_quantity,
                        row_number() OVER(PARTITION BY item_id ORDER BY buyout_copper//quantity,
                            (buyout_copper%quantity)::DOUBLE/quantity,sequence) AS floor_rank
                    FROM priced
                ), floor AS (
                    SELECT *,buyout_copper::DOUBLE/quantity min_unit_copper,
                        (CAST(buyout_copper AS HUGEINT)-CAST(vendor_sell_copper AS HUGEINT)*quantity)::DOUBLE/quantity difference_copper,
                        (CAST(buyout_copper AS HUGEINT)-CAST(vendor_sell_copper AS HUGEINT)*quantity)*100.0/
                            (CAST(vendor_sell_copper AS HUGEINT)*quantity) markup_percent
                    FROM book WHERE floor_rank=1 AND vendor_sell_copper>0
                ) SELECT *,abs(markup_percent) distance_percent FROM floor WHERE true"""
                params = []
                if stackable_only:sql+=' AND stackable IS TRUE'
                for value,comparison in ((min_markup,'>='),(max_markup,'<=')):
                    if value is not None:
                        threshold=(Decimal('100')+Decimal(str(value)))*10000
                        if threshold!=threshold.to_integral_value():
                            raise ValueError('Наценка: не более четырёх знаков после запятой')
                        sql+=f' AND CAST(buyout_copper AS HUGEINT)*1000000 {comparison} CAST(vendor_sell_copper AS HUGEINT)*quantity*CAST(? AS HUGEINT)'
                        params.append(int(threshold))
                if q:
                    pattern = '%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
                    sql += " AND (CAST(item_id AS VARCHAR)=? OR name ILIKE ? ESCAPE '\\' OR item_id IN (SELECT unnest(?)))"
                    params += [q, pattern, translated]
                if max_age_seconds:
                    sql += ' AND observed_at>=?'
                    from datetime import timedelta
                    params.append(now-timedelta(seconds=max_age_seconds))
                totals = rows('SELECT count(*) items,coalesce(sum(available_quantity),0) quantity,coalesce(sum(markup_percent<0),0) below_npc,coalesce(sum(markup_percent=0),0) equal_npc,coalesce(sum(markup_percent>0),0) above_npc FROM ('+sql+')', params)[0]
                selected = rows(sql+f' ORDER BY {columns[sort]} {order},item_id LIMIT ? OFFSET ?', [*params,limit,offset])
                listing=[]
                for row in selected:
                    listing.append({key:row[key] for key in ('item_id','name','observed_at','min_unit_copper','difference_copper',
                        'markup_percent','distance_percent','vendor_sell_copper','vendor_source','vendor_phase','available_quantity','stackable')})
                    listing[-1].update(special_variant=variant_key(row['item_id'],row['variant'])!=str(row['item_id']),
                        variant_name=row['name'],owned_at_min=row['owned'])
            finally:
                cursor.close()
        return {'format_version':2,'items': listing, 'total': totals['items'], 'summary': totals, 'snapshot': head[0] if head else None,
            'market': w.market_key,'min_markup_percent':min_markup,'max_markup_percent':max_markup,'as_of': now,'stackable_only':stackable_only,
            'stale_after_seconds': w.effective_config().max_price_age_seconds, 'offset': offset, 'limit': limit}

# EOF
