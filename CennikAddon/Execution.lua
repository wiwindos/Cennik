local _, C = ...
local E = {serial=0}
C.Execution = E
function E:Unknown(reason)
  local pending=C.db.pending
  if not pending then return end
  if pending.sniper then return C.Sniper:PurchaseUnknown(reason) end
  pending.stage="unknown"
  local task=C.db.tasks[pending.task_id]
  C:Emit(task,"purchase","unknown",{attempt_id=pending.attempt_id,reason=reason},pending.item_id)
  C:State(task,"requires_manual_check",reason .. "; повторная покупка заблокирована")
end
function E:Start() -- called only from the player's Buy button
  local ticket=C.BuyQueue.ticket
  if not ticket or C.db.pending or C.db.queue_stopped then return false end
  local task=C.db.tasks[ticket.task_id]
  local current=C.Validator:Validate(task)
  if current.state~="ready_to_buy" or C:Now()-ticket.prepared_at>15 or not C.Adapter:Ready() then
    C.BuyQueue.ticket=nil; C:State(task,"requires_manual_check","Снова подготовьте покупку: скан, цена или условия изменились"); return false
  end
  local lot=ticket.lot
  local currentPlan=current.plans[1]
  local currentLot=currentPlan.commodity and {item_id=currentPlan.item_id,quantity=currentPlan.quantity,cost=currentPlan.cost}
    or currentPlan.lots[1]
  if not currentLot or currentLot.item_id~=lot.item_id or currentLot.quantity~=lot.quantity or currentLot.cost~=lot.cost then
    C.BuyQueue.ticket=nil; C:State(task,"pending_scan","Подготовленная цена/количество изменились; снова подготовьте покупку"); return false
  end
  if not lot.commodity and (currentLot.auction_id~=lot.auction_id or not C.Adapter:KeyEqual(currentLot.key,lot.key)
    or currentLot.vendor_sell_copper~=lot.vendor_sell_copper) then
    C.BuyQueue.ticket=nil; C:State(task,"pending_scan","Выбранный лот или NPC-цена изменились; повторите подготовку"); return false
  end
  if not lot.commodity then
    if not C_AuctionHouse.GetAuctionInfoByID then C:State(task,"requires_manual_check","Нет API повторной проверки AuctionID"); return false end
    if not C.Adapter:LotStillAvailable(lot) then
      C.BuyQueue.ticket=nil
      C:Emit(task,"purchase","price_changed",{auction_id=lot.auction_id},lot.item_id)
      C:State(task,"pending_scan","Лот изменился или недоступен"); return false
    end
  end
  if lot.cost>GetMoney() or lot.cost>C.db.packages[task.package_id].budget-C.db.packages[task.package_id].spent then return false end
  self.serial=self.serial+1
  local pending={task_id=task.task_id,item_id=lot.item_id,quantity=lot.quantity,cost=lot.cost,auction_id=lot.auction_id,
    commodity=lot.commodity,limit=lot.limit,attempt_id=C.db.device_id .. "/" .. (C.db.sequence+1),
    stage=lot.commodity and "await_quote" or "buying",started_at=C:Now(),validation=current}
  if task.strategy_id=="vendor_arbitrage" then
    pending.vendor_sell_copper=lot.vendor_sell_copper or currentPlan.lots[1].vendor_sell_copper
    pending.vendor_credit_quantity=math.min(lot.quantity,math.max(0,task.opportunity.quantity-(task.bought[task.item_id] or 0)))
  end
  C.db.pending=pending
  C.BuyQueue.ticket=nil
  local ok
  if lot.commodity then ok=C.Adapter:Call("StartCommoditiesPurchase",lot.item_id,lot.quantity)
  else ok=C.Adapter:Call("PlaceBid",lot.auction_id,lot.cost) end
  if not ok then self:Unknown("Игровой API отклонил вызов"); return false end
  self:Timeout(pending)
  C:Refresh()
  return true
end
function E:Timeout(pending)
  C_Timer.After(15,function()
    if C.db.pending~=pending then return end
    if pending.stage=="await_quote" or pending.stage=="quoted" then E:Stop()
    elseif pending.stage=="buying" then E:Unknown("Таймаут: результат покупки неизвестен") end
  end)
end
function E:Confirm() -- a separate real player click; never called by price events
  local p=C.db.pending
  if p and p.sniper then return C.Sniper:ConfirmPurchase() end
  if not p or p.stage~="quoted" or C:Now()-p.quoted_at>10 then return false end
  local task=C.db.tasks[p.task_id]
  local valid=C.Validator:Validate(task,true)
  local package=C.db.packages[task.package_id]
  if valid.state~="ready_to_buy" or p.cost>GetMoney() or p.cost>package.budget-package.spent
    or valid.remaining_cost_copper+valid.npc_cost_copper-p.original_cost+p.cost>GetMoney()
    or valid.remaining_cost_copper+valid.npc_cost_copper-p.original_cost+p.cost>package.budget-package.spent
    or valid.profit_copper+(p.original_cost-p.cost)<task.min_profit_copper then self:Stop(); return false end
  p.stage="buying"
  local ok=C.Adapter:Call("ConfirmCommoditiesPurchase",p.item_id,p.quantity)
  if not ok then self:Unknown("Подтверждение покупки отклонено API"); return false end
  C:Refresh()
  return true
end
function E:Finish(state,details)
  local p=C.db.pending
  if not p then return end
  local task=C.db.tasks[p.task_id]
  local success=state=="bought"
  if success then
    if task.strategy_id=="vendor_arbitrage" and p.vendor_sell_copper then
      task.vendor_bought_revenue=(task.vendor_bought_revenue or 0)+p.vendor_sell_copper*math.min(p.quantity,p.vendor_credit_quantity or p.quantity)
    end
    task.bought[p.item_id]=(task.bought[p.item_id] or 0)+p.quantity
    task.spent=task.spent+p.cost
    C.db.packages[task.package_id].spent=C.db.packages[task.package_id].spent+p.cost
  end
  details=details or {}; details.attempt_id=p.attempt_id; details.auction_id=p.auction_id
  C:Emit(task,"purchase",state,details,p.item_id,success and p.quantity or 0,success and p.cost or 0)
  C.db.pending=nil
  local complete=true
  for _, line in ipairs(task.lines) do
    if line.source=="auction" and (task.bought[line.item_id] or 0)<line.quantity then complete=false end
  end
  C:State(task,complete and "completed" or (task.spent>0 and "partially_bought" or "pending_scan"),
    complete and "Закупка AH завершена; крафт/продажа не выполнялись" or "Подготовьте следующий шаг вручную")
end
function E:Stop()
  local p=C.db.pending
  if not p then return end
  if p.sniper then return C.Sniper:StopPurchase() end
  if p.stage=="await_quote" or p.stage=="quoted" then
    C.Adapter:Call("CancelCommoditiesPurchase")
    self:Finish("cancelled")
  else self:Unknown("Операция уже отправлена; остановка не подтверждает отмену") end
end
function E:OnEvent(event,...)
  local p=C.db and C.db.pending
  if not p or p.external_conflict then return end
  if p.sniper then return C.Sniper:OnPurchaseEvent(event,...) end
  if (event=="ADDON_ACTION_BLOCKED" or event=="ADDON_ACTION_FORBIDDEN") and ...~="CennikAddon" then return end
  if event=="COMMODITY_PURCHASE_SUCCEEDED" or event=="COMMODITY_PURCHASE_FAILED" then
    local item=...
    if item~=nil and item~=p.item_id then return end
  end
  if event=="COMMODITY_PRICE_UPDATED" and p.commodity and p.stage=="await_quote" then
    local unit,total=...
    local task=C.db.tasks[p.task_id]
    local validation=p.validation
    if not C:Integer(unit) or not C:Integer(total) or unit>p.limit or total>p.limit*p.quantity
      or total>GetMoney() or total>C.db.packages[task.package_id].budget-C.db.packages[task.package_id].spent
      or validation.cost_copper-p.cost+total>task.max_total_cost_copper
      or validation.remaining_cost_copper+validation.npc_cost_copper-p.cost+total>GetMoney()
      or validation.remaining_cost_copper+validation.npc_cost_copper-p.cost+total>C.db.packages[task.package_id].budget-C.db.packages[task.package_id].spent
      or validation.profit_copper+p.cost-total<task.min_profit_copper then
      C.Adapter:Call("CancelCommoditiesPurchase")
      self:Finish("price_changed"); return
    end
    p.original_cost,p.cost,p.stage,p.quoted_at=p.cost,total,"quoted",C:Now()
    C:Refresh()
  elseif event=="COMMODITY_PRICE_UNAVAILABLE" and p.commodity and p.stage=="await_quote" then
    C.Adapter:Call("CancelCommoditiesPurchase"); self:Finish("insufficient_supply")
  elseif (p.stage=="buying" or p.stage=="unknown") then
    if p.commodity and event=="COMMODITY_PURCHASE_SUCCEEDED" then self:Finish("bought")
    elseif p.commodity and event=="COMMODITY_PURCHASE_FAILED" then self:Finish("api_error")
    elseif not p.commodity and event=="AUCTION_HOUSE_PURCHASE_COMPLETED" then
      local auctionID=...
      if auctionID==nil or auctionID==p.auction_id then self:Finish("bought") end
    elseif event=="AUCTION_HOUSE_SHOW_ERROR" or event=="ADDON_ACTION_BLOCKED" or event=="ADDON_ACTION_FORBIDDEN" then
      self:Unknown("AH/защищённый API сообщил ошибку; проверьте фактический результат")
    end
  end
end
function E:Reconcile(bought,quantity,cost) -- explicit player declaration after mailbox/history check
  local p=C.db.pending
  if not p or p.stage~="unknown" then return false end
  if p.sniper then return C.Sniper:Reconcile(bought,quantity,cost) end
  if bought then
    if not C:Integer(quantity) or not C:Integer(cost) then return false end
    if not p.commodity and quantity~=1 then return false end
    p.quantity,p.cost=quantity,cost
  end
  self:Finish(bought and "bought" or "not_bought",{manually_confirmed=true})
  return true
end
local frame=CreateFrame("Frame")
for _, event in ipairs({"COMMODITY_PRICE_UPDATED","COMMODITY_PRICE_UNAVAILABLE","COMMODITY_PURCHASE_SUCCEEDED",
  "COMMODITY_PURCHASE_FAILED","AUCTION_HOUSE_PURCHASE_COMPLETED","AUCTION_HOUSE_SHOW_ERROR","ADDON_ACTION_BLOCKED","ADDON_ACTION_FORBIDDEN"}) do
  pcall(frame.RegisterEvent,frame,event)
end
frame:SetScript("OnEvent",function(_,event,...) E:OnEvent(event,...) end)
