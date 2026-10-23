local _, C = ...
local S=C.Sniper
local function reject(reason) S:Notice(reason); return false,reason end
function S:PurchaseUnknown(reason)
  local p=C.db.pending
  if not p or not p.sniper then return end
  if p.stage~="unknown" then self:Emit("purchase",{state="unknown",rule_id=p.rule_id,attempt_id=p.attempt_id,item_id=p.item_id}) end
  p.stage="unknown"
  self.overlayHidden=false; self.overlayRequested=true
  self:Notice(reason .. ". Повтор заблокирован: проверьте почту/историю и уточните результат")
end
function S:FinishPurchase(state,manual)
  local p=C.db.pending
  if not p or not p.sniper then return end
  local rule=C.db.sniper.rules[p.rule_id]
  if state=="bought" then
    rule.bought=rule.bought+p.quantity; rule.spent=rule.spent+p.cost
    C.db.sniper.spent=C.db.sniper.spent+p.cost
    if self:Remaining(rule)==0 then rule.enabled=false end
  end
  self:Emit("purchase",{state=state,rule_id=p.rule_id,attempt_id=p.attempt_id,item_id=p.item_id,
    auction_id=p.auction_id,quantity=state=="bought" and p.quantity or 0,cost_copper=state=="bought" and p.cost or 0,
    manually_confirmed=manual==true,remaining=self:Remaining(rule),surplus=math.max(0,rule.bought-rule.goal)})
  self.alerts[p.rule_id]=nil
  local book=self.findings[p.rule_id]
  if book then
    for _,snapshot in pairs(book.keys) do
      if p.identity then snapshot.rows[p.identity]=nil end
    end
  end
  C.db.pending=nil; self.pauseUntil=GetTime()+1
  self.overlayHidden=false; self.overlayRequested=true
  if state=="bought" then
    self:Notice("|cff66ff99Куплено: " .. C.Items:Label(p.item_id) .. " × " .. p.quantity .. ". Потрачено " .. GetMoneyString(p.cost,true) .. "; осталось " .. self:Remaining(rule) .. " шт.|r")
  else
    local labels={cancelled="Котировка отменена. Покупка не отправлена",not_bought="Вы отметили: не куплено",api_error="Игра отклонила покупку. Предмет не куплен"}
    self:Notice(labels[state] or ("Покупка: " .. state))
  end
  if C.UI and C.UI.section=="Sniper" then C:Refresh() end
end
function S:Buy(identity)
  local lot=self.alerts[identity or self.selected]
  local rule=lot and C.db.sniper.rules[lot.rule_id]
  if C.db.pending then return reject("Предыдущая покупка ещё не завершена; смотрите её результат в окне") end
  if not rule then return reject("Выбранный лот недоступен. Дождитесь проверки или выберите «Другой лот»") end
  if not rule.enabled then return reject("Цель приостановлена. Включите поиск кнопкой «Пуск»") end
  if self.current then self:CancelSearch("Проверка приостановлена для покупки выбранной находки"); self.pauseUntil=GetTime()+5 end
  if not C.Adapter:Ready("sniper") then return reject("Аукцион занят другой операцией. Покупка не отправлена; нажмите снова после её завершения") end
  if not C:MarketBindingMatches(rule.definition.market) or C.db.sniper.workspace_id~=C.db.workspace_id then return reject("Подтвердите рынок и рабочее пространство. Покупка не отправлена") end
  if #C.db.sniper.events>=1900 then return reject("Сначала сохраните и синхронизируйте журнал. Покупка не отправлена") end
  if lot.cost>GetMoney() then return reject("На этом персонаже недостаточно золота для покупки. Покупка не отправлена; поиск продолжается") end
  local limit=self:Limit(rule)
  if self:Remaining(rule)==0 or not limit or lot.unit_price>limit or C:Now()-lot.at>15
    or not C.Adapter:LotStillAvailable(lot) then
    self.alerts[lot.rule_id]=nil; rule.checked_at=nil
    return reject("Лот устарел либо цена, количество или условия изменились. Покупка не отправлена; ждём свежую проверку")
  end
  local p=C:Clone(lot)
  p.sniper=true; p.started_at=C:Now(); p.stage=lot.commodity and "await_quote" or "buying"
  p.attempt_id=C.db.device_id .. ":snipe-buy:" .. ((C.db.sniper.sequence or 0)+1)
  C.db.pending=p
  self.overlayHidden=false; self.overlayRequested=true
  self:Emit("purchase",{state="started",rule_id=p.rule_id,attempt_id=p.attempt_id,item_id=p.item_id})
  local ok
  if p.commodity then ok=C.Adapter:Call("StartCommoditiesPurchase",p.item_id,p.quantity)
  else ok=C.Adapter:Call("PlaceBid",p.auction_id,p.cost) end
  if not ok then self:PurchaseUnknown("API отклонил вызов"); return false,self.message end
  C_Timer.After(15,function()
    if C.db.pending~=p then return end
    if p.stage=="await_quote" or p.stage=="quoted" then S:StopPurchase(); S:Notice("Котировка не подтверждена за 15 секунд. Покупка не отправлена")
    elseif p.stage=="buying" then S:PurchaseUnknown("Нет подтверждения покупки") end
  end)
  self:Notice(p.commodity and "Получаем котировку: требуется отдельное подтверждение" or "Покупка отправлена; ждём подтверждения")
  return true
end
function S:ConfirmPurchase()
  local p=C.db.pending
  if not p or not p.sniper or p.stage~="quoted" then return reject("Нет котировки для подтверждения") end
  local rule=C.db.sniper.rules[p.rule_id]
  local limit=rule and self:Limit(rule)
  if C:Now()-p.quoted_at>3 then self:StopPurchase(); return reject("Котировка старше 3 секунд. Покупка не отправлена; получите новую котировку") end
  if p.cost>GetMoney() then self:StopPurchase(); return reject("Для этой котировки недостаточно золота. Покупка не отправлена; поиск продолжается") end
  if not rule or not rule.enabled or not limit or p.unit_price>limit
    or not C:MarketBindingMatches(rule.definition.market) or not C.Adapter:Ready("sniper") then
    self:StopPurchase(); return reject("Котировка или условия изменились. Покупка не отправлена")
  end
  p.stage="buying"
  local ok=C.Adapter:Call("ConfirmCommoditiesPurchase",p.item_id,p.quantity)
  if not ok then self:PurchaseUnknown("API не подтвердил вызов"); return false,self.message end
  self:Notice("Подтверждение отправлено; ждём результата")
  return true
end
function S:StopPurchase()
  local p=C.db.pending
  if not p or not p.sniper then return end
  if p.stage=="await_quote" or p.stage=="quoted" then
    C.Adapter:Call("CancelCommoditiesPurchase"); self:FinishPurchase("cancelled")
  else self:PurchaseUnknown("Покупка уже отправлена; отмена не подтверждена") end
end
function S:Reconcile(bought,quantity,cost)
  local p=C.db.pending
  if not p or not p.sniper or p.stage~="unknown" then return false end
  if bought then
    if not C:Integer(quantity) or not C:Integer(cost) then return false end
    if not p.commodity and quantity~=1 then self:Notice("Один AuctionID — одна вещь. Остальные покупки учитывайте отдельно"); return false end
    p.quantity,p.cost=quantity,cost
  end
  self:FinishPurchase(bought and "bought" or "not_bought",true)
  return true
end
function S:OnPurchaseEvent(event,...)
  local p=C.db.pending
  if not p or not p.sniper or p.external_conflict then return end
  if (event=="ADDON_ACTION_BLOCKED" or event=="ADDON_ACTION_FORBIDDEN") and ...~="CennikAddon" then return end
  if event=="COMMODITY_PRICE_UPDATED" and p.commodity and p.stage=="await_quote" then
    local unit,total=...
    if not C:Integer(unit) or not C:Integer(total) then self:StopPurchase(); self:Notice("Игра вернула некорректную котировку. Покупка не отправлена"); return end
    if unit>p.limit or math.ceil(total/p.quantity)>p.limit then self:StopPurchase(); self:Notice("Текущая цена превысила ваш предел. Покупка не отправлена"); return end
    if total>GetMoney() then self:StopPurchase(); self:Notice("Для текущей котировки недостаточно золота. Покупка не отправлена; поиск продолжается"); return end
    p.unit_price,p.cost,p.stage,p.quoted_at=unit,total,"quoted",C:Now()
    self:Notice("Котировка получена. Проверьте полную стоимость и подтвердите")
  elseif event=="COMMODITY_PRICE_UNAVAILABLE" and p.commodity and p.stage=="await_quote" then self:StopPurchase(); self:Notice("Товар по этой цене уже недоступен. Покупка не отправлена")
  elseif p.stage=="buying" then
    if p.commodity and (event=="COMMODITY_PURCHASE_SUCCEEDED" or event=="COMMODITY_PURCHASE_FAILED") then
      local item=...; if item~=nil and item~=p.item_id then return end
      self:FinishPurchase(event=="COMMODITY_PURCHASE_SUCCEEDED" and "bought" or "api_error")
    elseif not p.commodity and event=="AUCTION_HOUSE_PURCHASE_COMPLETED" then
      local auction=...
      if auction==p.auction_id then self:FinishPurchase("bought")
      elseif auction==nil then self:PurchaseUnknown("Клиент не сообщил AuctionID") end
    elseif event=="AUCTION_HOUSE_SHOW_ERROR" or event=="ADDON_ACTION_BLOCKED" or event=="ADDON_ACTION_FORBIDDEN" then
      self:PurchaseUnknown("Клиент сообщил ошибку покупки")
    end
  end
end
