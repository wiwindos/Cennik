local _, C = ...
local S,U=C.Sniper,C.UI
local panel,text,button=S.MakePanel,S.MakeText,S.MakeButton
local modes={any="Любая цена",limit="Не дороже за штуку",median3d="Скидка к медиане 3 дней"}
local function money(value) return value and GetMoneyString(value,true) or "неизвестно" end
function S:ShowFindings(identity)
  self.overlayHidden=false; self.overlayRequested=true
  if identity then self.selected=identity end
  self:RenderOverlay()
end
function S:FindingState(rule)
  if self:Remaining(rule)==0 then return "Цель выполнена" end
  if not rule.enabled then return "Пауза" end
  local lot=self.alerts[rule.definition.id]
  if rule.refreshing or self.current and self.current.rule==rule then return lot and "Обновляю проверку" or "Ищу" end
  if lot then return C:Now()-lot.at<=15 and "Найдено" or "Нужна свежая проверка" end
  if rule.last_result and rule.last_result.quantity_warning then return "Нужна проверка конкретного лота" end
  if rule.search_error then return "Проверка не завершена" end
  if rule.last_match==false then return "Предложение больше не доступно" end
  return "Ищу"
end
function S:RenderOverlay()
  if not C.db or not C.db.sniper then return end
  if U.section=="Sniper" then for _,entry in ipairs(U.sniperRows or {}) do entry.font:SetText(self:RowText(entry.rule)) end end
  if self.overlayHidden then if self.overlay then self.overlay:Hide() end; return end
  local rules=self:Rules(); local running,found=0,0
  for _,rule in ipairs(rules) do
    if rule.enabled and self:Remaining(rule)>0 then running=running+1 end
    local lot=self.alerts[rule.definition.id]
    if lot and C:Now()-lot.at<=15 then found=found+1 end
  end
  local pending=C.db.pending
  if not C.Adapter:Open() or (not self.overlayRequested and running==0 and found==0 and not (pending and pending.sniper)) then
    if self.overlay then self.overlay:Hide() end; return
  end
  local p=self.overlay
  if not p then
    p=panel(500,455); self.overlay=p; p:ClearAllPoints(); p:SetPoint("TOPRIGHT",AuctionHouseFrame,"TOPRIGHT",-15,-15)
    p.onUserClose=function() S.overlayHidden=true; S.overlayRequested=false end
    p.title=text(p,"Cennik · Снайпер",14,-13,440)
    p.scroll=CreateFrame("ScrollFrame",nil,p,"UIPanelScrollFrameTemplate"); p.scroll:SetPoint("TOPLEFT",14,-41); p.scroll:SetSize(447,108); p.scroll:SetClipsChildren(true)
    p.body=CreateFrame("Frame",nil,p.scroll); p.body:SetSize(435,108); p.scroll:SetScrollChild(p.body); p.rows={}
    p.content=text(p,"",14,-164,468); p.content:SetHeight(184); p.content:SetJustifyV("TOP")
    p.buy=button(p,"Нет находки",14,-362,174,function()
      local current=C.db.pending; local ok,reason
      if current and current.sniper and current.stage=="quoted" then ok,reason=S:ConfirmPurchase()
      else ok,reason=S:Buy(S.selected) end
      if not ok then S:Notice(reason or "Покупка недоступна") end
    end)
    button(p,"Пропустить",196,-362,138,function()
      if C.db.pending then if C.db.pending.sniper then S:StopPurchase() end; return end
      local rule=C.db.sniper.rules[S.selected or ""]
      if rule then rule.snooze=GetTime()+30; rule.refreshing=false; S.alerts[S.selected]=nil end
    end)
    button(p,"Пауза",342,-362,144,function() S:Pause() end)
    button(p,"Список целей",14,-397,215,function() U:Attach(); U:Show(); U:SelectSection("Sniper") end)
    p.reconcile=button(p,"Уточнить результат",238,-397,248,function() S:ShowReconcile() end)
    p.footer=text(p,"Закрытие окна не останавливает поиск. Открыть снова: «Находки».",14,-433,468)
  end
  p:Show(); p.title:SetText("Cennik · Снайпер · находок: " .. found)
  local selected=C.db.sniper.rules[self.selected or ""]
  if not selected or selected.removed then selected=rules[1]; self.selected=selected and selected.definition.id end
  for i,rule in ipairs(rules) do
    local row=p.rows[i]
    if not row then row=button(p.body,"",0,-(i-1)*33,433,function() end); row:SetHeight(31); p.rows[i]=row end
    row:Show()
    row:SetText((self.selected==rule.definition.id and "▶ " or "") .. C.Items:Label(rule.definition.item_id) .. " · нужно " .. self:Remaining(rule) .. " · " .. self:FindingState(rule))
    row:SetScript("OnClick",function() S.selected=rule.definition.id; S:RenderOverlay() end)
  end
  for i=#rules+1,#p.rows do p.rows[i]:Hide() end
  p.body:SetHeight(math.max(108,#rules*33)); p.scroll:UpdateScrollChildRect()
  p.reconcile:Hide()
  if pending and pending.sniper then
    p.content:SetText("Покупка: " .. C.Items:Label(pending.item_id) .. " (#" .. pending.item_id .. ")\nКоличество: " .. pending.quantity .. " шт.; всего " .. money(pending.cost) .. "\n" .. (self.message or "Ожидание подтверждения"))
    p.buy:SetText(pending.stage=="quoted" and "Подтвердить покупку" or "Ожидание результата")
    p.buy:SetEnabled(pending.stage=="quoted" and C:Now()-pending.quoted_at<=3)
    if pending.stage=="unknown" then p.reconcile:Show() end
  elseif selected then
    local lot=self.alerts[selected.definition.id]
    local previous=lot or selected.last_finding
    local condition=modes[selected.definition.mode]
    if selected.definition.mode~="any" then condition=condition .. ": ≤ " .. money(self:Limit(selected)) .. "/шт." end
    if selected.definition.mode=="median3d" and selected.reference then
      local ref=selected.reference
      condition=condition .. "\nОриентир: " .. money(ref.copper) .. " · " .. (ref.source or "история") .. "\n" .. tostring(ref.start):sub(1,10) .. " — до " .. tostring(ref['end']):sub(1,10) .. " UTC"
    end
    local detail=C.Items:Label(selected.definition.item_id) .. " (#" .. selected.definition.item_id .. ")\n" .. self:FindingState(selected) .. "; требуется " .. self:Remaining(selected) .. " шт.\n" .. condition
    if previous then
      detail=detail .. "\n" .. (lot and "Найденный лот: " or "Прошлая находка, сейчас не подтверждена: ") .. previous.quantity .. " шт. · " .. money(previous.unit_price) .. "/шт. · всего " .. money(previous.cost)
      detail=detail .. "\nПроверен " .. math.max(0,math.floor(C:Now()-previous.at)) .. " с назад" .. (previous.auction_id and (" · AuctionID " .. previous.auction_id) or "")
      if previous.quantity>self:Remaining(selected) then detail=detail .. "\nНеделимый лот: избыток " .. previous.quantity-self:Remaining(selected) .. " шт." end
      if lot and lot.cost>GetMoney() then detail=detail .. "\nНа этом персонаже недостаточно золота; поиск продолжается." end
    else detail=detail .. "\n" .. (selected.reason or "Предложения ещё не проверены") end
    if not lot and selected.last_result and selected.last_result.quantity_warning then detail=detail .. "\n" .. selected.last_result.quantity_warning end
    p.content:SetText(detail)
    p.buy:SetText(lot and ("Купить " .. lot.quantity .. " шт.") or "Нет доступного лота")
    -- Keep the action stable while background queries refresh; a click has priority.
    p.buy:SetEnabled(lot~=nil and selected.enabled and self:Remaining(selected)>0 and C:Now()-lot.at<=15 and self:Limit(selected)~=nil and not C.db.pending)
  else
    p.content:SetText("Целей нет. Добавьте предмет в снайпер и нажмите «Пуск»."); p.buy:SetText("Нет находки"); p.buy:Disable()
  end
  if U.sniperStatus and U.section=="Sniper" then U.sniperStatus:SetText("Активных целей: " .. running .. "; найдено: " .. found .. "; запросов / 60 с: " .. self:Traffic()) end
end
