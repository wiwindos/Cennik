local _, C = ...
local V = {maxAge=90}
C.Validator = V
function V:Part(value,numerator,denominator)
  if not C:Integer(value,true) or not C:Integer(numerator,true) or not C:Integer(denominator) then return nil end
  return math.floor(value/denominator)*numerator + math.floor((value%denominator)*numerator/denominator)
end
function V:Fresh(scan)
  return scan and scan.complete and scan.observed_at<=C:Now() and C:Now()-scan.observed_at<=self.maxAge
end
function V:PlanScan(task,line,needed,live,scan)
  if not self:Fresh(scan) then return nil,"pending_scan","Нужен свежий полный скан " .. C.Items:Name(line.item_id) end
  local plan={item_id=line.item_id,quantity=0,needed=needed,cost=0,lots={},commodity=scan.commodity,
    limit=line.unit_limit_copper,key=scan.key,vendor_revenue=0,max_unit_price=0}
  if needed==0 then return plan end
  local eligible,available,uncertain,seen={},0,false,{}
  for _,row in ipairs(scan.rows) do
    if task.strategy_id=="vendor_arbitrage" then
      local sell,source=C.NPC:Vendor(task,row)
      row.vendor_known=sell~=nil; row.vendor_sell_copper=sell; row.vendor_source=source
    end
    if not row.ownership_known then uncertain=true end
    if row.ownership_known and not row.owned and C:Integer(row.quantity) and C:Integer(row.total_price)
      and C:Integer(row.unit_price) and (live or row.unit_price<=line.unit_limit_copper) then
      if not C.Adapter:KeyEqual(row.item_key,scan.key) then uncertain=true
      elseif task.strategy_id=="vendor_arbitrage" and not row.vendor_known then uncertain=true
      elseif task.strategy_id~="vendor_arbitrage" or (row.vendor_sell_copper or 0)>0 then
        if row.commodity or C:Integer(row.auction_id) and not seen[row.auction_id] then
          if not row.commodity then seen[row.auction_id]=true end
          local depth=row.available_quantity or row.quantity
          if C:Integer(depth) then eligible[#eligible+1]=row; available=available+depth else uncertain=true end
        end
      end
    end
  end
  -- A group is depth, never an indivisible purchase. Keep its representative ID
  -- for the next one-item action; every subsequent action gets a new live scan.
  table.sort(eligible,function(a,b)
    local av,bv=a.unit_price,b.unit_price
    if task.strategy_id=="vendor_arbitrage" then av=av-a.vendor_sell_copper; bv=bv-b.vendor_sell_copper end
    if av~=bv then return av<bv end
    return (a.auction_id or 0)<(b.auction_id or 0)
  end)
  local chosen,remaining={},needed
  for _,row in ipairs(eligible) do
    local quantity=math.min(remaining,row.available_quantity or row.quantity)
    chosen[#chosen+1]={row=row,quantity=quantity,credit_quantity=quantity}; remaining=remaining-quantity
    if remaining==0 then break end
  end
  if remaining>0 then chosen=nil end
  if not chosen then
    local name=C.Items:Name(line.item_id) .. " (#" .. line.item_id .. ")"
    return nil,uncertain and "requires_manual_check" or "insufficient_supply",uncertain and (name .. ": неизвестно владение, вариант или NPC-цена")
      or (name .. ": нужно " .. needed .. ", доступно " .. available .. (live and " по текущим ценам" or (" в пределах " .. GetMoneyString(line.unit_limit_copper,true))) .. "; полного плана закупки нет")
  end
  for _,entry in ipairs(chosen) do
    local row,quantity=entry.row,entry.quantity
    local cost=quantity*row.unit_price
    plan.quantity,plan.cost=plan.quantity+quantity,plan.cost+cost
    plan.max_unit_price=math.max(plan.max_unit_price,row.unit_price)
    plan.vendor_revenue=plan.vendor_revenue+(row.vendor_sell_copper or 0)*entry.credit_quantity
    plan.lots[#plan.lots+1]={item_id=line.item_id,quantity=row.commodity and quantity or 1,cost=row.commodity and cost or row.unit_price,auction_id=row.auction_id,
      planned_quantity=quantity,available_quantity=row.available_quantity or row.quantity,
      commodity=row.commodity,key=scan.key,limit=line.unit_limit_copper,observed_at=scan.observed_at,
      vendor_sell_copper=row.vendor_sell_copper,vendor_source=row.vendor_source,item_link=row.item_link}
  end
  if not C:Integer(plan.cost) or not C:Integer(plan.vendor_revenue,true) or not C:Integer(plan.quantity) then return nil,"requires_manual_check","Стоимость или количество вне точного диапазона" end
  plan.extra_quantity=math.max(0,plan.quantity-needed)
  return plan
end
function V:Plan(task,line,needed,live)
  local scan=task.scans[line.item_id]
  if task.strategy_id=="vendor_arbitrage" and scan and scan.variants then
    if not self:Fresh(scan) then return nil,"pending_scan","Нужен свежий полный скан всех вариантов" end
    local best,reason,state
    for _,variant in ipairs(scan.variants) do
      local plan,s,r=self:PlanScan(task,line,needed,live,variant)
      if plan and (not best or plan.vendor_revenue-plan.cost>best.vendor_revenue-best.cost
        or (plan.vendor_revenue-plan.cost==best.vendor_revenue-best.cost and plan.cost<best.cost)) then best=plan end
      if not plan then state,reason=s,r end
    end
    return best,best and nil or state or "insufficient_supply",best and nil or reason or "Подходящих лотов нет"
  end
  if scan and task.selected_keys[line.item_id] and not C.Adapter:KeyEqual(task.selected_keys[line.item_id],scan.key) then
    return nil,"pending_scan","Выбран другой вариант; повторите скан"
  end
  return self:PlanScan(task,line,needed,live,scan)
end
function V:Validate(task,ownPending)
  local result={checked_at=C:Now(),materials={},plans={},complete=false,blockers={}}
  local function rejected(state,reason) result.state,result.reason=state,reason; return result end
  local ok,reason=C:CheckTask(task,ownPending)
  if not ok then return rejected("requires_manual_check",reason) end
  local o=task.opportunity
  if not o or o.execution_status~="indicative" then return rejected("requires_manual_check","Неверный исходный контракт") end
  local remainingCost,total,npc,vendorRevenue=0,task.spent or 0,0,task.vendor_bought_revenue or 0
  local missingState,missingReason
  local missing={}
  local function miss(state,reason)
    missing[#missing+1]=reason
    missingState,missingReason=state,table.concat(missing,"; ")
  end
  for _,line in ipairs(task.lines) do
    if not C:Integer(line.quantity) or not C:Integer(line.unit_limit_copper,true) then return rejected("requires_manual_check","Неверные количества или пределы") end
    if line.source=="npc" then
      local quote=C.NPC:Quote(task,line.item_id)
      result.materials[#result.materials+1]={item_id=line.item_id,needed=line.quantity,source="npc",quote=quote}
      if not quote then miss("requires_manual_check","Не подтверждена текущая цена NPC: " .. C.Items:Name(line.item_id) .. ". Откройте торговца или введите цену в «Цены / лимиты».")
      elseif quote.available and quote.available<line.quantity then miss("insufficient_supply","У NPC недостаточно " .. C.Items:Name(line.item_id))
      else
        local cost=C.NPC:Cost(quote,line.quantity)
        if cost==nil then miss("requires_manual_check","NPC-стоимость вне точного диапазона copper")
        else npc=npc+cost end
      end
    elseif line.source=="auction" then
      local needed=math.max(0,line.quantity-(task.bought[line.item_id] or 0))
      if needed>0 then
        local plan,state,note=self:Plan(task,line,needed,true)
        result.materials[#result.materials+1]={item_id=line.item_id,needed=needed,source="auction",plan=plan,reason=note}
        if not plan then miss(state,note)
        else
          remainingCost=remainingCost+plan.cost; vendorRevenue=vendorRevenue+plan.vendor_revenue
          result.plans[#result.plans+1]=plan
          if plan.max_unit_price>line.unit_limit_copper then result.blockers[#result.blockers+1]=C.Items:Name(line.item_id) .. ": текущая цена выше предела покупки" end
        end
      end
    else return rejected("requires_manual_check","Неизвестный источник материала") end
  end
  result.ah_cost_copper=remainingCost
  if missingReason then return rejected(missingState,missingReason) end
  total=total+remainingCost+npc
  if not C:Integer(total) then return rejected("requires_manual_check","Стоимость вне точного диапазона copper") end
  local gross
  if task.strategy_id=="vendor_arbitrage" then
    if (task.bought[task.item_id] or 0)>0 and task.vendor_bought_revenue==nil then return rejected("requires_manual_check","Для прежней частичной покупки неизвестна фактическая NPC-цена") end
    gross=vendorRevenue
    local sources={}
    for _,plan in ipairs(result.plans) do for _,lot in ipairs(plan.lots) do sources[lot.vendor_source or "неизвестно"]=true end end
    local names={}; for source in pairs(sources) do names[#names+1]=source end
    result.revenue_basis="NPC: " .. table.concat(names,", ") .. "; лишние единицы не добавлены к прибыли"
  elseif task.strategy_id=="historical_discount" then result.revenue_basis="Текущий минимум AH; история — только ориентир скидки"
  else result.revenue_basis="Текущий минимум AH; будущая продажа не подтверждена" end
  if task.strategy_id~="vendor_arbitrage" then
    local scan=task.scans[task.item_id]
    if not self:Fresh(scan) then return rejected("pending_scan","Нужен свежий скан результата") end
    local sale
    for _,row in ipairs(scan.rows) do
      if row.ownership_known and not row.owned and row.quantity>0 then
        local price=row.unit_price
        if price>0 then sale=sale and math.min(sale,price) or price end
      end
    end
    if not sale then return rejected("requires_manual_check","Готовый товар отсутствует в текущих предложениях: цена продажи неизвестна") end
    gross=sale*o.quantity; result.sale_unit_copper=sale
  end
  if not C:Integer(gross,true) then return rejected("requires_manual_check","Выручка вне точного диапазона copper") end
  local cut=task.strategy_id=="vendor_arbitrage" and 0 or self:Part(gross,task.fee_numerator,task.fee_denominator)
  if cut==nil then return rejected("requires_manual_check","Неизвестна комиссия") end
  local net=gross-cut
  result.complete=true
  result.cost_copper,result.remaining_cost_copper,result.npc_cost_copper=total,remainingCost,npc
  result.gross_revenue_copper,result.net_revenue_copper,result.fee_copper=gross,net,cut
  result.profit_copper=net-total; result.roi_percent=result.profit_copper*100/total
  result.margin_percent=gross>0 and result.profit_copper*100/gross or nil
  local feeRate=task.strategy_id=="vendor_arbitrage" and 0 or task.fee_numerator
  local breakEven=self:Part(total,task.fee_denominator,task.fee_denominator-feeRate)
  if breakEven then
    local after=task.strategy_id=="vendor_arbitrage" and breakEven or breakEven-self:Part(breakEven,feeRate,task.fee_denominator)
    if after<total then breakEven=breakEven+1 end
    result.break_even_unit_copper=math.ceil(breakEven/o.quantity)
  end
  if result.profit_copper<task.min_profit_copper then return rejected("not_profitable","По текущим ценам прибыль ниже порога после комиссии") end
  if task.strategy_id=="historical_discount" then
    local reference=o.details.history and o.details.history.copper
    local maxCost=reference and self:Part(reference*o.quantity,task.discount_denominator-task.discount_numerator,task.discount_denominator)
    if not maxCost or total>maxCost then return rejected("not_profitable","Текущая закупка не достигает исторического дисконта") end
  end
  if total>task.max_total_cost_copper then result.blockers[#result.blockers+1]="Превышен общий предел затрат задания" end
  if #result.blockers>0 then return rejected("limits_exceeded",table.concat(result.blockers,"; ") .. ". Экономика рассчитана; покупка заблокирована.") end
  local package=C.db.packages[task.package_id]
  if not package or remainingCost+npc>package.budget-package.spent or remainingCost+npc>GetMoney() then
    return rejected("requires_manual_check","Текущая экономика рассчитана; не хватает золота или общего бюджета (включая NPC)")
  end
  if (npc>0 or task.strategy_id=="simple_craft" or task.strategy_id=="craft_graph") and not task.conditions_confirmed then return rejected("requires_manual_check","Подтвердите доступ к профессии, рецепту, NPC и откаты") end
  result.state=#result.plans==0 and "completed" or "ready_to_buy"
  result.reason="Закупка проверена по текущим лотам; крафт и продажа не выполнялись"
  return result
end
