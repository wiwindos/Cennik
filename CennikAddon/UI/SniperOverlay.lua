local _, C = ...
local S,U=C.Sniper,C.UI
local panel,text=S.MakePanel,S.MakeText
local function button(...)
  local frame=S.MakeButton(...)
  local font=frame:GetFontString()
  if font then
    local path,size,flags=font:GetFont()
    if path and size then font:SetFont(path,math.max(9,size-1),flags) end
  end
  return frame
end
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
local function readable(font,size)
  font:SetFont(STANDARD_TEXT_FONT or "Fonts\\FRIZQT__.TTF",(size or 14)-1,"")
  font:SetSpacing(3)
end
function S:UpdateFindingBorder()
  local p=self.overlay
  if not p then return end
  local active=self.findingHighlight~=nil
  local age=type(p.flashStarted)=="number" and GetTime()-p.flashStarted or 1
  local lit=active and (age>=1 or age<0.5)
  if p.borderLit==lit and p.borderActive==active then return end
  p.borderLit,p.borderActive=lit,active
  for _,edge in ipairs(p.redBorder) do edge:SetAlpha(active and (lit and 1 or 0.12) or 0) end
end
function S:RenderFindingSignal()
  local p=self.overlay
  local finding=self.findingHighlight
  if finding and p.signalID~=self.findingSignal then
    p.signalID=self.findingSignal; p.flashStarted=GetTime()
  end
  local shown,layout={},{}
  for index,notice in ipairs(self.findingNotices or {}) do
    local id=notice.finding.rule_id
    local icon=p.foundIcons[id]
    if not icon then
      icon=CreateFrame("Button",nil,p.iconsBody); p.foundIcons[id]=icon
      icon:SetSize(48,48); icon.ruleID=id
      local border=icon:CreateTexture(nil,"BACKGROUND"); border:SetAllPoints(icon)
      border:SetColorTexture(1,0.25,0.12,1)
      icon.image=icon:CreateTexture(nil,"ARTWORK")
      icon.image:SetPoint("TOPLEFT",2,-2); icon.image:SetPoint("BOTTOMRIGHT",-2,2)
      icon:HookScript("OnMouseDown",function() S:FocusWindow(p) end)
      icon:SetScript("OnClick",function(self)
        S:FocusWindow(p); S.selected=self.ruleID
        S:ReadFinding(self.ruleID); S:RenderOverlay()
      end)
      icon:SetScript("OnEnter",function(self)
        local current=S.unreadFindings[self.ruleID]
        if current and GameTooltip then
          GameTooltip:SetOwner(self,"ANCHOR_RIGHT")
          GameTooltip:SetText(C.Items:Name(current.finding.item_id))
          GameTooltip:AddLine(current.message,1,0.82,0,true)
          GameTooltip:AddLine("Нажмите, чтобы открыть цель и отметить изменение просмотренным.",1,1,1,true)
          GameTooltip:Show()
        end
      end)
      icon:SetScript("OnLeave",function() if GameTooltip then GameTooltip:Hide() end end)
    end
    local info=C.Items:Get(notice.finding.item_id)
    local texture=info and info.icon or "Interface\\Icons\\INV_Misc_QuestionMark"
    if icon.texture~=texture then icon.texture=texture; icon.image:SetTexture(texture) end
    if icon.layoutIndex~=index then
      icon.layoutIndex=index; icon:ClearAllPoints()
      icon:SetPoint("TOPLEFT",(index-1)%p.iconsPerRow*56,-math.floor((index-1)/p.iconsPerRow)*56)
    end
    visible(icon,true); shown[id]=true; layout[#layout+1]=id
  end
  for id,icon in pairs(p.foundIcons) do if not shown[id] then visible(icon,false) end end
  local signature=table.concat(layout,"|")
  if p.iconsLayout~=signature then
    p.iconsLayout=signature
    p.iconsBody:SetHeight(math.max(48,math.ceil(#layout/p.iconsPerRow)*56))
    p.iconsScroll:UpdateScrollChildRect()
    p.iconsScroll:SetVerticalScroll(math.min(p.iconsScroll:GetVerticalScroll(),math.max(0,p.iconsScroll:GetVerticalScrollRange())))
  end
  self:UpdateFindingBorder()
end
function S:ShowFindings(identity)
  self.overlayHidden=false; self.overlayRequested=true
  if identity then self.selected=identity end
  self:RenderOverlay()
  if self.overlay then self:OpenWindow(self.overlay) end
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
  self.overlayHidden=false; self.overlayRequested=true; self:ClosePopups()
  self:FocusWindow(AuctionHouseFrame)
  self:Notice("Предложения открыты в «Купить». Окно снайпера остаётся открытым; поиск на паузе, чтобы не мешать выбору лота.")
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
    local width,height=self:PanelSize(960,760)
    local left=math.floor((width-54)*0.38); local rightX=left+36; local right=width-rightX-18
    p=panel(width,height); self.overlay=p
    p.redBorder={}
    for _,side in ipairs({"TOP","BOTTOM","LEFT","RIGHT"}) do
      local edge=p:CreateTexture(nil,"OVERLAY"); edge:SetTexture("Interface\\Buttons\\WHITE8X8"); edge:SetVertexColor(1,0.12,0.12,1)
      if side=="TOP" or side=="BOTTOM" then
        edge:SetPoint(side .. "LEFT",p,side .. "LEFT",0,0); edge:SetPoint(side .. "RIGHT",p,side .. "RIGHT",0,0); edge:SetHeight(3)
      else
        edge:SetPoint("TOP" .. side,p,"TOP" .. side,0,0); edge:SetPoint("BOTTOM" .. side,p,"BOTTOM" .. side,0,0); edge:SetWidth(3)
      end
      edge:SetAlpha(0); p.redBorder[#p.redBorder+1]=edge
    end
    p:SetScript("OnUpdate",function() S:UpdateFindingBorder() end)
    p.onUserClose=function() S.overlayHidden=true; S.overlayRequested=false end
    p.title=text(p,"Cennik · Снайпер",18,-16,width-70); readable(p.title,17)
    p.resumeAll=button(p,"Продолжить все",18,-48,170,function() S:ResumeAll(); C:Refresh() end)
    p.pauseAll=button(p,"Пауза все",198,-48,130,function() S:Pause(); C:Refresh() end)
    p.add=button(p,"Добавить",338,-48,110,function() S:ShowEditor() end)
    button(p,"Все цели",458,-48,130,function() U:Attach(); U:Show(); U:SelectSection("Sniper") end)
    p.resume=p.resumeAll
    local listTitle=text(p,"Цели поиска · выберите предмет",18,-91,left); readable(listTitle)
    p.scroll=CreateFrame("ScrollFrame",nil,p,"UIPanelScrollFrameTemplate"); p.scroll:SetPoint("TOPLEFT",18,-116); p.scroll:SetSize(left-18,height-190); p.scroll:SetClipsChildren(true)
    p.rowWidth=left-30
    p.body=CreateFrame("Frame",nil,p.scroll); p.body:SetSize(p.rowWidth,height-190); p.scroll:SetScrollChild(p.body); p.rows={}
    p.finding=text(p,"",rightX,-94,right); readable(p.finding,14); p.finding:SetHeight(30); p.finding:SetJustifyV("TOP")
    p.iconsScroll=CreateFrame("ScrollFrame",nil,p,"UIPanelScrollFrameTemplate")
    p.iconsScroll:SetPoint("TOPLEFT",rightX,-130); p.iconsScroll:SetSize(right-24,52); p.iconsScroll:SetClipsChildren(true)
    p.iconsPerRow=math.max(1,math.floor((right-24)/56))
    p.iconsBody=CreateFrame("Frame",nil,p.iconsScroll); p.iconsBody:SetSize(right-24,48)
    p.iconsScroll:SetScrollChild(p.iconsBody); p.foundIcons={}
    p.latest=button(p,"Показать новое",rightX,-194,right,function() S:ShowLatestFinding() end)
    p.contentScroll=CreateFrame("ScrollFrame",nil,p,"UIPanelScrollFrameTemplate")
    p.contentScroll:SetPoint("TOPLEFT",rightX,-234); p.contentScroll:SetSize(right-18,height-450); p.contentScroll:SetClipsChildren(true)
    p.contentBody=CreateFrame("Frame",nil,p.contentScroll); p.contentBody:SetSize(right-30,height-450)
    p.contentScroll:SetScrollChild(p.contentBody)
    p.content=text(p.contentBody,"",0,0,right-30); readable(p.content,15); p.content:SetJustifyV("TOP"); p.content:SetWordWrap(true)
    p.notice=text(p,"",rightX,-height+210,right); readable(p.notice,14); p.notice:SetHeight(56); p.notice:SetJustifyV("TOP")
    p.buy=button(p,"Нет находки",rightX,-height+146,right,function()
      local current=C.db.pending; local ok,reason
      if current and current.sniper and current.stage=="quoted" then ok,reason=S:ConfirmPurchase()
      else ok,reason=S:Buy(S.selected) end
      if not ok then S:Notice(reason or "Покупка недоступна") end
    end)
    p.best=button(p,"Самый дешёвый",rightX,-height+110,(right-8)/2,function()
      S:SelectCheapestFinding(C.db.sniper.rules[S.selected or ""])
    end)
    p.next=button(p,"Другой лот",rightX+(right+8)/2,-height+110,(right-8)/2,function()
      local rule=C.db.sniper.rules[S.selected or ""]
      if rule and not C.db.pending then S:NextFinding(rule) end
    end)
    p.toggle=button(p,"Пауза",rightX,-height+74,(right-16)/3,function()
      local rule=C.db.sniper.rules[S.selected or ""]
      if not rule then return end
      if C.db.pending then S:Notice("Сначала завершите покупку"); return end
      if not C:MarketBindingMatches(rule.definition.market) then S:Notice("Подтвердите рынок"); return end
      rule.enabled=not rule.enabled and S:Remaining(rule)>0
      if rule.enabled then S:ResumeSearch()
      else S:CancelSearch("Цель приостановлена"); S:Notice("Цель приостановлена") end
      if U.section=="Sniper" then C:Refresh() end
    end)
    p.edit=button(p,"Изменить",rightX+(right+8)/3,-height+74,(right-16)/3,function()
      local rule=C.db.sniper.rules[S.selected or ""]
      if rule then S:ShowEditor(rule.definition.item_id,rule) end
    end)
    p.remove=button(p,"Удалить",rightX+2*(right+8)/3,-height+74,(right-16)/3,function() S:RemoveGoal(C.db.sniper.rules[S.selected or ""]) end)
    p.reconcile=button(p,"Уточнить результат",rightX,-height+110,right,function() S:ShowReconcile() end)
    p.cancel=button(p,"Отменить котировку",rightX,-height+110,right,function() S:StopPurchase() end)
    p.offers=button(p,"Посмотреть лоты",rightX,-height+38,right,function() S:ShowOffers() end)
    p.footer=text(p,"",18,-height+64,left); readable(p.footer,13); p.footer:SetHeight(50); p.footer:SetJustifyV("TOP")
  end
  if not p:IsShown() then self:OpenWindow(p,true) end
  setText(p.title,"Cennik · Снайпер · целей: " .. #rules .. " · с предложением: " .. found)
  local selected=C.db.sniper.rules[self.selected or ""]
  if not selected or selected.removed then selected=rules[1]; self.selected=selected and selected.definition.id end
  local layout={}; local y=0
  for i,rule in ipairs(rules) do
    local row=p.rows[i]
    if not row then
      row=button(p.body,"",0,0,p.rowWidth,function(self)
        S.selected=self.ruleID; S:ReadFinding(self.ruleID); S:RenderOverlay()
      end)
      row.marker=text(row,"",6,-6,16); readable(row.marker,14)
      row.title=text(row,"",24,-6,p.rowWidth-32); readable(row.title,14); row.title:SetWordWrap(true)
      row.detail=text(row,"",24,-28,p.rowWidth-32); readable(row.detail,13); row.detail:SetWordWrap(true)
      p.rows[i]=row
    end
    row.ruleID=rule.definition.id
    if not row:IsShown() then row:Show() end
    local unread=self.unreadFindings[rule.definition.id]~=nil
    setText(row.marker,self.selected==rule.definition.id and "▶" or unread and "●" or "")
    if row.noticeUnread~=unread then
      row.noticeUnread=unread; row.title:SetTextColor(1,unread and 0.8 or 1,unread and 0.35 or 1)
    end
    local titleChanged=setText(row.title,C.Items:Label(rule.definition.item_id))
    local detailChanged=setText(row.detail,"Нужно " .. self:Remaining(rule) .. " · " .. self:FindingState(rule))
    if titleChanged or detailChanged or not row.rowHeight then
      row.title:SetHeight(0); row.detail:SetHeight(0)
      local titleHeight=row.title:GetStringHeight()
      row.title:SetHeight(titleHeight); row.detail:ClearAllPoints(); row.detail:SetPoint("TOPLEFT",24,-titleHeight-10)
      local detailHeight=row.detail:GetStringHeight(); row.detail:SetHeight(detailHeight)
      row.rowHeight=math.max(58,titleHeight+detailHeight+18); row:SetHeight(row.rowHeight)
    end
    if row.layoutY~=y then row.layoutY=y; row:ClearAllPoints(); row:SetPoint("TOPLEFT",0,y) end
    layout[#layout+1]=rule.definition.id .. ":" .. row.rowHeight; y=y-row.rowHeight-6
  end
  for i=#rules+1,#p.rows do if p.rows[i]:IsShown() then p.rows[i]:Hide() end end
  local signature=table.concat(layout,"|")
  if p.layout~=signature then
    p.layout=signature; p.body:SetHeight(math.max(p.scroll:GetHeight(),-y)); p.scroll:UpdateScrollChildRect()
    p.scroll:SetVerticalScroll(math.min(p.scroll:GetVerticalScroll(),math.max(0,p.scroll:GetVerticalScrollRange())))
  end
  visible(p.reconcile,pending and pending.sniper and pending.stage=="unknown")
  visible(p.cancel,pending and pending.sniper and (pending.stage=="await_quote" or pending.stage=="quoted"))
  visible(p.next,not (pending and pending.sniper)); visible(p.best,not (pending and pending.sniper)); visible(p.latest,self.latestFinding~=nil)
  enabled(p.resumeAll,not pending); enabled(p.pauseAll,not pending)
  self:RenderFindingSignal()
  local notice=(self.unreadCount or 0)>0 and ("Непросмотренных целей: " .. self.unreadCount .. ". Нажмите иконку предмета.")
    or ((self.findingSignal or 0)>0 and "Все изменения просмотрены." or "Первый скан покажет уже выставленное. Звук — при новых предложениях.")
  setText(p.finding,"|cff66ff99" .. notice .. "|r")
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
  local checked=self.lastScanAt and date("%H:%M:%S",self.lastScanAt) or "ещё не выполнена"
  local footer="Поиск: " .. running .. " целей.\nПоследняя проверка: " .. checked
  if self.manualHold then
    footer="Поиск на паузе. Цены могут устареть.\nПоследняя проверка: " .. checked .. "\nДля запуска: «Продолжить все»."
  end
  setText(p.footer,footer)
  if U.sniperStatus and U.section=="Sniper" then
    setText(U.sniperStatus,self.manualHold and "Поиск на паузе после ручной работы в аукционе." or ("Активных целей: " .. running .. "; с предложением: " .. found .. ". Цена в правилах — за штуку."))
  end
end
