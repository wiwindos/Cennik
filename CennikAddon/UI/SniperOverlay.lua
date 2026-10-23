local _, C = ...
local S,U=C.Sniper,C.UI
local panel,text,button=S.MakePanel,S.MakeText,S.MakeButton
local modes={any="Любая цена",limit="Не дороже за штуку",median3d="Скидка к медиане 3 дней"}
local function money(value) return value and GetMoneyString(value,true) or "неизвестно" end
-- Tick may call rendering often; unchanged controls must not be rewritten.
local function setText(frame,value)
  if frame.cennikText~=value then frame.cennikText=value; frame:SetText(value); return true end
end
local function enabled(frame,value)
  value=not not value
  if frame:IsEnabled()~=value then frame:SetEnabled(value) end
end
local function visible(frame,value)
  if value and not frame:IsShown() then frame:Show() elseif not value and frame:IsShown() then frame:Hide() end
end
function S:ShowFindings(identity)
  self.overlayHidden=false; self.overlayRequested=true
  if identity then self.selected=identity end
  self:RenderOverlay()
end
function S:ShowOffers()
  local rule=C.db.sniper.rules[self.selected or ""]
  if not rule or rule.removed then self:Notice("Сначала выберите цель"); return false end
  if C.db.pending then self:Notice("Сначала завершите или уточните покупку"); return false end
  local lot=self.alerts[rule.definition.id] or rule.last_finding
  local def=rule.definition
  local key=lot and lot.key
  if not key and def.item_level~=nil then
    key={itemID=def.item_id,itemLevel=def.item_level,itemSuffix=def.item_suffix,battlePetSpeciesID=def.pet_id}
  end
  if not key and rule.keys and #rule.keys==1 then key=rule.keys[1] end
  if not key and C.Adapter:Equipment(def.item_id)==false then key=C_AuctionHouse.MakeItemKey(def.item_id) end
  if not key then self:Notice("Сначала проверьте цель и выберите нужный вариант предмета"); return false end
  self:HoldForManualSearch("Просмотр всех предложений")
  local ok,reason=C.Adapter:OpenOffers(key,lot and lot.unit_price)
  if not ok then self:Notice(reason); return false end
  self.overlayHidden=true; self.overlayRequested=false; self:ClosePopups(true)
  self:Notice("Открыты все предложения в «Купить». Снайпер на паузе; возврат: /cennik sniper → «Находки»")
  return true
end
function S:SetFindingDetails(value,identity)
  local p=self.overlay
  local changed=setText(p.content,value)
  local same=p.detailIdentity==identity
  if not changed and same then return end
  local offset=same and p.contentScroll:GetVerticalScroll() or 0
  p.detailIdentity=identity
  p.content:SetHeight(0)
  local height=p.content:GetStringHeight()+8
  p.content:SetHeight(height); p.contentBody:SetHeight(math.max(p.contentScroll:GetHeight(),height))
  p.contentScroll:UpdateScrollChildRect()
  p.contentScroll:SetVerticalScroll(math.min(offset,math.max(0,p.contentScroll:GetVerticalScrollRange())))
end
function S:FindingState(rule)
  if self:Remaining(rule)==0 then return "Цель выполнена" end
  if not rule.enabled then return "Пауза" end
  local lot=self.alerts[rule.definition.id]
  if lot then return ({new="Новый лот",existing="Уже есть на аукционе",changed="Цена изменилась",supply="Предложение обновилось"})[lot.event] or "Найдено" end
  if rule.last_finding then return self:FindingBook(rule).initialized and "Выбранный лот недоступен" or "Ожидаю свежую проверку" end
  if rule.last_result and rule.last_result.quantity_warning then return "Размер лота не подтверждён" end
  if rule.last_match==false then return "Подходящих предложений нет" end
  return "Ожидаю предложение"
end
function S:RemoveGoal(rule)
  if not rule then return end
  if C.db.pending then self:Notice("Сначала завершите или уточните покупку"); return end
  if #C.db.sniper.events>=1900 then self:Notice("Сначала синхронизируйте журнал"); return end
  rule.enabled=false; rule.removed=true; self.alerts[rule.definition.id]=nil
  self:ResetFindings(rule.definition.id); self:Emit("rule_remove",{rule_id=rule.definition.id})
  self:Notice("Цель удалена; история покупок сохранена")
  if U.section=="Sniper" then C:Refresh() end
end
function S:RenderOverlay()
  if not C.db or not C.db.sniper then return end
  if U.section=="Sniper" then for _,entry in ipairs(U.sniperRows or {}) do setText(entry.font,self:RowText(entry.rule)) end end
  if self.overlayHidden then if self.overlay and self.overlay:IsShown() then self.overlay:Hide() end; return end
  local rules=self:Rules(); local running,found=0,0
  for _,rule in ipairs(rules) do
    if rule.enabled and self:Remaining(rule)>0 then running=running+1 end
    if rule.enabled and self.alerts[rule.definition.id] then found=found+1 end
  end
  local pending=C.db.pending
  if not C.Adapter:Open() or (not self.overlayRequested and running==0 and found==0 and not (pending and pending.sniper)) then
    if self.overlay and self.overlay:IsShown() then self.overlay:Hide() end; return
  end
  local p=self.overlay
  if not p then
    p=panel(570,610); self.overlay=p; p:ClearAllPoints(); p:SetPoint("TOPRIGHT",AuctionHouseFrame,"TOPRIGHT",-15,-15)
    p.onUserClose=function() S.overlayHidden=true; S.overlayRequested=false end
    p.title=text(p,"Cennik · Снайпер",14,-13,520)
    text(p,"Цели · выберите строку. Поиск не зависит от золота персонажа.",14,-36,535)
    p.scroll=CreateFrame("ScrollFrame",nil,p,"UIPanelScrollFrameTemplate"); p.scroll:SetPoint("TOPLEFT",14,-57); p.scroll:SetSize(517,113); p.scroll:SetClipsChildren(true)
    p.body=CreateFrame("Frame",nil,p.scroll); p.body:SetSize(505,113); p.scroll:SetScrollChild(p.body); p.rows={}
    p.finding=text(p,"",14,-182,370); p.finding:SetHeight(30); p.finding:SetJustifyV("TOP")
    p.latest=button(p,"Показать новое",394,-184,162,function() S:ShowLatestFinding() end)
    p.contentScroll=CreateFrame("ScrollFrame",nil,p,"UIPanelScrollFrameTemplate")
    p.contentScroll:SetPoint("TOPLEFT",14,-220); p.contentScroll:SetSize(517,154); p.contentScroll:SetClipsChildren(true)
    p.contentBody=CreateFrame("Frame",nil,p.contentScroll); p.contentBody:SetSize(505,154)
    p.contentScroll:SetScrollChild(p.contentBody)
    p.content=text(p.contentBody,"",0,0,505); p.content:SetJustifyV("TOP"); p.content:SetWordWrap(true)
    p.notice=text(p,"",14,-385,535); p.notice:SetHeight(52); p.notice:SetJustifyV("TOP")
    p.buy=button(p,"Нет находки",14,-445,270,function()
      local current=C.db.pending; local ok,reason
      if current and current.sniper and current.stage=="quoted" then ok,reason=S:ConfirmPurchase()
      else ok,reason=S:Buy(S.selected) end
      if not ok then S:Notice(reason or "Покупка недоступна") end
    end)
    p.best=button(p,"Самый дешёвый",294,-445,136,function()
      S:SelectCheapestFinding(C.db.sniper.rules[S.selected or ""])
    end)
    p.next=button(p,"Другой лот",440,-445,116,function()
      local rule=C.db.sniper.rules[S.selected or ""]
      if rule and not C.db.pending then S:NextFinding(rule) end
    end)
    p.toggle=button(p,"Пауза",14,-482,88,function()
      local rule=C.db.sniper.rules[S.selected or ""]
      if not rule then return end
      if C.db.pending then S:Notice("Сначала завершите покупку"); return end
      if not C:MarketBindingMatches(rule.definition.market) then S:Notice("Подтвердите рынок"); return end
      rule.enabled=not rule.enabled and S:Remaining(rule)>0
      if rule.enabled then S:ResumeSearch()
      else S:CancelSearch("Цель приостановлена"); S:Notice("Цель приостановлена") end
      if U.section=="Sniper" then C:Refresh() end
    end)
    p.edit=button(p,"Изменить",108,-482,100,function()
      local rule=C.db.sniper.rules[S.selected or ""]
      if rule then S:ShowEditor(rule.definition.item_id,rule) end
    end)
    p.remove=button(p,"Удалить",214,-482,90,function() S:RemoveGoal(C.db.sniper.rules[S.selected or ""]) end)
    button(p,"Добавить",310,-482,100,function() S:ShowEditor() end)
    button(p,"Все цели",416,-482,140,function() U:Attach(); U:Show(); U:SelectSection("Sniper") end)
    p.reconcile=button(p,"Уточнить результат",359,-445,197,function() S:ShowReconcile() end)
    p.cancel=button(p,"Отменить котировку",359,-445,197,function() S:StopPurchase() end)
    p.resume=button(p,"Продолжить поиск",14,-523,220,function() S:ResumeSearch(); C:Refresh() end)
    p.offers=button(p,"Все предложения → Купить",244,-523,312,function() S:ShowOffers() end)
    p.footer=text(p,"",14,-563,535); p.footer:SetHeight(34); p.footer:SetJustifyV("TOP")
  end
  if not p:IsShown() then p:Show() end
  setText(p.title,"Cennik · Снайпер · целей: " .. #rules .. " · с предложением: " .. found)
  local selected=C.db.sniper.rules[self.selected or ""]
  if not selected or selected.removed then selected=rules[1]; self.selected=selected and selected.definition.id end
  local layout={}
  for i,rule in ipairs(rules) do
    layout[#layout+1]=rule.definition.id
    local row=p.rows[i]
    if not row then
      row=button(p.body,"",0,-(i-1)*33,503,function(self) S.selected=self.ruleID; S:RenderOverlay() end)
      row:SetHeight(31); p.rows[i]=row
    end
    row.ruleID=rule.definition.id
    if not row:IsShown() then row:Show() end
    setText(row,(self.selected==rule.definition.id and "▶ " or "") .. C.Items:Label(rule.definition.item_id) .. " · нужно " .. self:Remaining(rule) .. " · " .. self:FindingState(rule))
  end
  for i=#rules+1,#p.rows do if p.rows[i]:IsShown() then p.rows[i]:Hide() end end
  local signature=table.concat(layout,"|")
  if p.layout~=signature then p.layout=signature; p.body:SetHeight(math.max(113,#rules*33)); p.scroll:UpdateScrollChildRect() end
  visible(p.reconcile,pending and pending.sniper and pending.stage=="unknown")
  visible(p.cancel,pending and pending.sniper and (pending.stage=="await_quote" or pending.stage=="quoted"))
  visible(p.next,not (pending and pending.sniper)); visible(p.best,not (pending and pending.sniper)); visible(p.latest,self.latestFinding~=nil)
  visible(p.resume,self.manualHold~=nil); enabled(p.resume,not pending)
  setText(p.finding,"|cff66ff99" .. (self.findingNotice or "Первый скан покажет уже выставленное. Звук — при новых предложениях.") .. "|r")
  -- Polling never overwrites transaction feedback.
  setText(p.notice,self.message and ("Результат действия: " .. self.message) or "Выберите предложение. Покупка выполняется только вашим нажатием.")
  enabled(p.toggle,selected~=nil and not pending); enabled(p.edit,selected~=nil and not pending); enabled(p.remove,selected~=nil and not pending)
  enabled(p.offers,selected~=nil and not pending)
  setText(p.toggle,selected and selected.enabled and "Пауза" or "Пуск")
  if pending and pending.sniper then
    local description=({await_quote="Получаем котировку. Покупка ещё не отправлена.",quoted="Проверьте цену и нажмите «Подтвердить».",buying="Запрос покупки отправлен. Ждём ответа игры.",unknown="Результат неизвестен. Повторная покупка заблокирована."})[pending.stage]
    self:SetFindingDetails(C.Items:Label(pending.item_id) .. " (#" .. pending.item_id .. ")\nКоличество: " .. pending.quantity .. " шт.\nЗа штуку: " .. money(pending.unit_price) .. "\nПолная стоимость: " .. money(pending.cost) .. "\n" .. (description or "Ожидание ответа"),pending.attempt_id or "pending")
    setText(p.buy,pending.stage=="quoted" and ("Подтвердить за " .. money(pending.cost)) or "Ожидание результата…")
    enabled(p.buy,pending.stage=="quoted")
  elseif selected then
    local lot=self.alerts[selected.definition.id]; local previous=lot or selected.last_finding
    local cheapest=self:CheapestFinding(selected)
    local condition=modes[selected.definition.mode]
    if selected.definition.mode~="any" then condition=condition .. ": ≤ " .. money(self:Limit(selected)) .. "/шт." end
    if selected.definition.mode=="median3d" and selected.reference then
      condition=condition .. "\nМедиана: " .. money(selected.reference.copper) .. " · " .. tostring(selected.reference.start):sub(1,10) .. " — " .. tostring(selected.reference['end']):sub(1,10) .. " UTC"
    end
    local detail=C.Items:Label(selected.definition.item_id) .. " (#" .. selected.definition.item_id .. ")\n" .. self:FindingState(selected) .. "; требуется " .. self:Remaining(selected) .. " шт.\n" .. condition
    if previous then
      detail=detail .. "\n" .. (lot and "Выбранное предложение: " or "Прошлая находка, сейчас не подтверждена: ") .. previous.quantity .. " шт.\nЗа штуку: " .. money(previous.unit_price) .. " · Всего: " .. money(previous.cost)
      if previous.commodity then detail=detail .. "\nТовар: цена будет повторно подтверждена котировкой" end
      detail=detail .. "\nДоступно по этой цене: " .. (previous.available or previous.quantity) .. " шт." .. (previous.commodity and "" or "; одна покупка = 1 шт.")
      if lot and lot.cost>GetMoney() then detail=detail .. "\nДля покупки здесь не хватает золота; поиск продолжается." end
      if not lot and (selected.offer_count or 0)>0 then detail=detail .. "\nЕсть другие предложения: нажмите «Другой лот»." end
    elseif selected.last_result and selected.last_result.quantity_warning then detail=detail .. "\n" .. selected.last_result.quantity_warning end
    if cheapest then
      if previous and cheapest.unit_price<previous.unit_price then
        detail=detail .. "\n|cffffcc00Есть дешевле: " .. money(cheapest.unit_price) .. "/шт. → «Самый дешёвый».|r"
      else detail=detail .. "\nМинимум по проверенным предложениям: " .. money(cheapest.unit_price) .. "/шт." end
    end
    if selected.definition.mode=="median3d" and not self:Limit(selected) then detail=detail .. "\nНужна история трёх завершённых дней из веба." end
    self:SetFindingDetails(detail,selected.definition.id)
    setText(p.buy,lot and ("Купить " .. lot.quantity .. " шт. за " .. money(lot.cost)) or "Нет выбранного лота")
    enabled(p.buy,lot~=nil and selected.enabled and self:Remaining(selected)>0)
    setText(p.next,"Другой лот (" .. (selected.offer_count or 0) .. ")"); enabled(p.next,(selected.offer_count or 0)>0)
    enabled(p.best,cheapest~=nil)
  else
    self:SetFindingDetails("Целей нет. Нажмите «Добавить», выберите предмет и включите поиск.","empty")
    setText(p.buy,"Нет находки"); enabled(p.buy,false); enabled(p.next,false); enabled(p.best,false)
  end
  local footer="Поиск: " .. running .. " целей. Последняя проверка: " .. (self.lastScanAt and date("%H:%M:%S",self.lastScanAt) or "ещё не выполнена")
  if self.manualHold then
    footer="Поиск на паузе: " .. self.manualHold .. ".\nПоследняя проверка: " .. (self.lastScanAt and date("%H:%M:%S",self.lastScanAt) or "ещё не выполнена") .. "; найденные цены могут устареть."
  else
    if selected and selected.search_error then footer=footer .. " · " .. selected.search_error end
    footer=footer .. "\nЗакрытие окна не останавливает поиск. Открыть: «Находки»."
  end
  setText(p.footer,footer)
  if U.sniperStatus and U.section=="Sniper" then
    setText(U.sniperStatus,self.manualHold and "Поиск на паузе после ручной работы в аукционе." or ("Активных целей: " .. running .. "; с предложением: " .. found .. ". Цена в правилах — за штуку."))
  end
end
