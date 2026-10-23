local _, C = ...
local S,U=C.Sniper,C.UI
local modeNames={any="Любая цена",limit="Не дороже / шт.",median3d="Скидка к медиане 3 дней"}
local priorities={high="Высокий",normal="Обычный",low="Низкий"}
local function money(n) return n and GetMoneyString(n,true) or "нет данных" end
local function text(parent,value,x,y,width)
  local f=parent:CreateFontString(nil,"OVERLAY","GameFontHighlightSmall")
  if type(parent.cennikWidth)=="number" and y>=-30 then width=math.min(width or 420,parent.cennikWidth-x-36) end
  f:SetPoint("TOPLEFT",x,y); f:SetWidth(width or 420); f:SetJustifyH("LEFT"); f:SetText(value); return f
end
local function button(parent,value,x,y,width,callback)
  local f=CreateFrame("Button",nil,parent,"UIPanelButtonTemplate")
  f:SetSize(width,25); f:SetPoint("TOPLEFT",x,y); f:SetText(value); f:SetScript("OnClick",callback); return f
end
local function input(parent,value,x,y,width,multiline)
  local f=CreateFrame("EditBox",nil,parent,"InputBoxTemplate")
  f:SetPoint("TOPLEFT",x,y); f:SetSize(width,25); f:SetAutoFocus(false); f:SetText(tostring(value or ""))
  if multiline then f:SetMultiLine(true) end
  return f
end
local function panel(width,height)
  local p=CreateFrame("Frame",nil,UIParent or AuctionHouseFrame,BackdropTemplateMixin and "BackdropTemplate" or nil)
  p:SetSize(width,height); p:SetPoint("CENTER"); p:SetFrameStrata("DIALOG"); p:EnableMouse(true)
  p.cennikWidth=width
  p:SetAlpha(1); p:SetToplevel(true)
  -- A real opaque texture also works when the client's Backdrop mixin is absent.
  local background=p:CreateTexture(nil,"BACKGROUND"); background:SetAllPoints(p)
  if background.SetColorTexture then background:SetColorTexture(0.04,0.08,0.11,1)
  else background:SetTexture("Interface\\Buttons\\WHITE8X8"); background:SetVertexColor(0.04,0.08,0.11,1) end
  p.cennikBackground=background
  if p.SetBackdrop then p:SetBackdrop({bgFile="Interface\\Tooltips\\UI-Tooltip-Background",edgeFile="Interface\\Tooltips\\UI-Tooltip-Border",tile=true,tileSize=16,edgeSize=16,insets={left=4,right=4,top=4,bottom=4}}); p:SetBackdropColor(0.04,0.08,0.11,0.98) end
  local close=CreateFrame("Button",nil,p,"UIPanelCloseButton"); close:SetSize(28,28); close:SetPoint("TOPRIGHT",-3,-3)
  close:SetScript("OnClick",function() if p.onUserClose then p.onUserClose() end; p:Hide() end); p.cennikClose=close
  S.popupFrames=S.popupFrames or {}; S.popupFrames[p]=true
  p:SetMovable(true); p:RegisterForDrag("LeftButton")
  p:SetScript("OnDragStart",function(self) self:StartMoving() end); p:SetScript("OnDragStop",function(self) self:StopMovingOrSizing() end)
  return p
end
S.MakePanel,S.MakeText,S.MakeButton,S.MakeInput=panel,text,button,input
function S:ClosePopups(all)
  for p in pairs(self.popupFrames or {}) do if all or p~=self.overlay then p:Hide() end end
end
function S:WatchAuctionTabs()
  if not self.libTabHook then
    local lib=LibStub and LibStub("LibAHTab-1-0",true)
    if lib and lib.SetSelected then hooksecurefunc(lib,"SetSelected",function() S:ClosePopups() end); self.libTabHook=true end
  end
  if not self.nativeTabHook and AuctionHouseFrame and AuctionHouseFrame.SetDisplayMode then
    hooksecurefunc(AuctionHouseFrame,"SetDisplayMode",function() S:ClosePopups() end); self.nativeTabHook=true
  end
end
function S:ShowEditor(item,existing,preset)
  if self.editor then self.editor:Hide() end
  local p=panel(510,430); self.editor=p
  local def=C:Clone(existing and existing.definition or preset or {item_id=item,quantity=1,mode="any",priority="normal",discount_bps=5000,unit_limit_copper=0,use_bags=false})
  text(p,existing and "Изменить цель снайпера" or "Добавить в снайпер",18,-18,440)
  text(p,"Название, ID, ссылка или Shift-клик в активное поле",18,-48,475)
  local entry=input(p,item or def.item_id,22,-70,320); entry:SetFocus()
  p.itemInfo=text(p,item and C.Items:Label(item) or "Нажмите «Найти», затем выберите предмет",18,-102,475)
  button(p,"Найти",355,-70,135,function()
    local ok,reason=S:Lookup(entry:GetText(),function(rows)
      S:ShowPicker(rows,function(id) entry:SetText(tostring(id)); p.itemInfo:SetText(C.Items:Label(id)) end)
    end)
    if not ok then p.itemInfo:SetText(reason) end
  end)
  if type(ChatEdit_InsertLink)=="function" and not self.linkHook then
    self.linkHook=true
    hooksecurefunc("ChatEdit_InsertLink",function(link)
      local editor=S.editor
      if editor and editor:IsShown() and editor.itemEntry and editor.itemEntry:HasFocus() then editor.itemEntry:SetText(link) end
    end)
  end
  p.itemEntry=entry
  if type(HandleModifiedItemClick)=="function" and not self.modifiedLinkHook then
    self.modifiedLinkHook=true
    hooksecurefunc("HandleModifiedItemClick",function(link)
      local editor=S.editor
      if IsShiftKeyDown() and editor and editor:IsShown() and editor.itemEntry:HasFocus() and type(link)=="string" then editor.itemEntry:SetText(link) end
    end)
  end
  entry:SetScript("OnReceiveDrag",function()
    if GetCursorInfo then local kind,id,link=GetCursorInfo(); if kind=="item" then entry:SetText(link or tostring(id)); ClearCursor() end end
  end)
  text(p,"Количество / потребность",18,-126,210); local qty=input(p,def.quantity,22,-146,130)
  button(p,"Карточка / рецепт",300,-120,190,function()
    local id=tonumber(entry:GetText()) or tonumber(entry:GetText():match("item:(%d+)"))
    if id then S:ShowCard(id) else p.itemInfo:SetText("Сначала найдите и выберите предмет") end
  end)
  local bags=CreateFrame("CheckButton",nil,p,"UICheckButtonTemplate"); bags:SetPoint("TOPLEFT",165,-143); bags:SetSize(26,26); bags:SetChecked(def.use_bags)
  text(p,"Вычесть сумки один раз",195,-151,285)
  local priceLabel=text(p,"",18,-223,470)
  local price=input(p,0,22,-245,180)
  local modeButtons={}
  local function refreshMode()
    for name,b in pairs(modeButtons) do b:SetText((def.mode==name and "✓ " or "") .. ({any="Любая цена",limit="Не дороже / шт.",median3d="Скидка, 3 дня"})[name]) end
    if def.mode=="any" then price:Hide(); priceLabel:SetText("Любая цена: сигнал о появлении, независимо от золота персонажа")
    else price:Show(); priceLabel:SetText(def.mode=="limit" and "Максимальная цена за штуку, золото" or "Скидка к медиане трёх завершённых дней, %") end
    price:SetText(tostring(def.mode=="median3d" and def.discount_bps/100 or def.unit_limit_copper/10000))
  end
  for index,name in ipairs({"any","limit","median3d"}) do
    modeButtons[name]=button(p,"",18+(index-1)*160,-185,155,function()
      local amount=tonumber((price:GetText():gsub(",","."))) or 0
      if def.mode=="limit" then def.unit_limit_copper=math.floor(amount*10000+0.5)
      elseif def.mode=="median3d" then def.discount_bps=math.floor(amount*100+0.5) end
      def.mode=name; refreshMode()
    end)
  end
  refreshMode(); p.modeButtons=modeButtons; p.priceInput=price
  local priority=button(p,priorities[def.priority],255,-245,235,function(self)
    def.priority=def.priority=="normal" and "high" or def.priority=="high" and "low" or "normal"; self:SetText(priorities[def.priority])
  end)
  priority:SetText(priorities[def.priority])
  local exact=CreateFrame("CheckButton",nil,p,"UICheckButtonTemplate"); exact:SetPoint("TOPLEFT",15,-280); exact:SetSize(26,26); exact:SetChecked(def.item_level~=nil)
  text(p,"Точный вариант из поиска Auctionator",45,-287,440)
  local note=text(p,"0.125 з = 12 с 50 м. Скидка 50%: медиана 2 з → порог 1 з.\nБез истории режим скидки недоступен. Сумки вычитаются один раз.",18,-317,475)
  button(p,"Сохранить цель",18,-380,220,function()
    local value=entry:GetText(); local id=tonumber(value) or tonumber(value:match("item:(%d+)"))
    local amount=def.mode=="any" and 0 or tonumber((price:GetText():gsub(",","."))); local quantity=tonumber(qty:GetText())
    if not id or not quantity or not amount or amount<0 then note:SetText("Проверьте ID, количество и цену/скидку"); return end
    if not existing then def.id=S:NewDefinition(id).id end
    def.market=C.db.current_market; def.item_id=id; def.quantity=quantity; def.use_bags=bags:GetChecked()==true
    def.unit_limit_copper=def.mode=="limit" and math.floor(amount*10000+0.5) or 0
    def.discount_bps=def.mode=="median3d" and math.floor(amount*100+0.5) or 5000
    if exact:GetChecked() then
      local key=C.lastSelectedKey
      if def.item_level==nil and (not key or key.itemID~=id) then note:SetText("Сначала выберите вариант в поиске Auctionator"); return end
      if def.item_level==nil and key and key.itemID==id then def.item_level=key.itemLevel or 0; def.item_suffix=key.itemSuffix or 0; def.pet_id=key.battlePetSpeciesID or 0 end
    else def.item_level=nil; def.item_suffix=nil; def.pet_id=nil end
    local ok,reason=S:Add(def)
    if not ok then note:SetText(reason); return end
    p:Hide(); U:SelectSection("Sniper")
  end)
  button(p,"Закрыть",320,-380,170,function() p:Hide() end)
end
function S:ShowTransfer()
  if self.transfer then self.transfer:Hide() end
  local p=panel(600,405); self.transfer=p
  text(p,"Список снайпера: экспорт / импорт",18,-18,550)
  local scroll=CreateFrame("ScrollFrame",nil,p,"UIPanelScrollFrameTemplate"); scroll:SetPoint("TOPLEFT",24,-48); scroll:SetSize(530,230); scroll:SetClipsChildren(true)
  local box=input(scroll,self:Export(),0,0,515,true); box:SetHeight(230); box:SetMaxLetters(200000)
  box:SetFontObject(GameFontHighlightSmall); box:SetJustifyH("LEFT"); box:SetJustifyV("TOP")
  scroll:SetScrollChild(box)
  box:SetScript("OnTextChanged",function(self) self:SetHeight(math.max(230,self:GetStringHeight()+20)); scroll:UpdateScrollChildRect() end)
  local note=text(p,"Скопируйте список или вставьте другой для предварительного просмотра.",18,-290,550)
  local preview,previewText
  button(p,"Проверить импорт",18,-335,180,function()
    local reason; preview,reason=S:Preview(box:GetText())
    previewText=preview and box:GetText() or nil
    note:SetText(preview and ("Целей: " .. #preview.rules .. ". Покупки сохранятся; новые цели приостановлены.") or reason)
  end)
  button(p,"Применить",210,-335,160,function()
    if not preview then note:SetText("Сначала проверьте импорт"); return end
    if previewText~=box:GetText() then note:SetText("Текст изменился; проверьте снова"); return end
    local ok,reason=S:Apply(preview); note:SetText(ok and "Список импортирован; сохраните /reload для веба" or reason)
    if ok then U:SelectSection("Sniper") end
  end)
  button(p,"Закрыть",400,-335,160,function() p:Hide() end)
end
function S:ShowReconcile()
  local pending=C.db.pending; if not pending or not pending.sniper or pending.stage~="unknown" then return end
  local p=panel(450,245)
  text(p,"Сначала проверьте почту, сумки и историю покупки.\nПодтверждайте фактически полученное количество и стоимость.",18,-20,410)
  text(p,"Количество",18,-80,180); text(p,"Стоимость, медь",220,-80,190)
  local quantity=input(p,pending.quantity,22,-105,160); local cost=input(p,pending.cost,225,-105,180)
  button(p,"Куплено",18,-155,125,function() if S:Reconcile(true,tonumber(quantity:GetText()),tonumber(cost:GetText())) then p:Hide() end end)
  button(p,"Не куплено",155,-155,135,function() S:Reconcile(false); p:Hide() end)
  button(p,"Закрыть",300,-155,130,function() p:Hide() end)
end
function S:ShowHistory()
  local p=panel(565,420)
  text(p,"Журнал снайпера · последние 200 событий",18,-18,520)
  local scroll=CreateFrame("ScrollFrame",nil,p,"UIPanelScrollFrameTemplate"); scroll:SetPoint("TOPLEFT",18,-48); scroll:SetSize(510,305); scroll:SetClipsChildren(true)
  local body=CreateFrame("Frame",nil,scroll); body:SetSize(495,305); scroll:SetScrollChild(body)
  local lines={}; local names={started="Отправлена",unknown="Результат неизвестен",bought="Куплено",not_bought="Не куплено",cancelled="Отменена",api_error="Ошибка API"}
  for i=#(C.db.sniper.history or {}),1,-1 do
    local event=C.db.sniper.history[i]
    if event.kind=="purchase" or event.kind=="external_purchase" then
      local state=event.kind=="external_purchase" and "Учтено вне Cennik" or (names[event.state] or event.state or "Событие")
      lines[#lines+1]=date("%d.%m %H:%M:%S",event.at) .. " · " .. state .. " · " .. C.Items:Name(event.item_id) ..
        (event.state=="bought" and (" × " .. event.quantity .. " · " .. money(event.cost_copper)) or event.kind=="external_purchase" and (" × " .. event.quantity .. " · стоимость неизвестна") or "") .. (event.manually_confirmed and " · уточнено игроком" or "")
    end
  end
  local label=text(body,#lines>0 and table.concat(lines,"\n\n") or "Покупок пока нет. Поиск сам по себе не подтверждает покупку.",0,0,490)
  body:SetHeight(math.max(305,label:GetStringHeight()+15)); scroll:UpdateScrollChildRect()
  button(p,"Закрыть",390,-375,150,function() p:Hide() end)
end
function S:RowText(rule)
  local def=rule.definition; local limit=self:Limit(rule)
  return C.Items:Label(def.item_id) .. "\n" .. priorities[def.priority] .. " · " .. modeNames[def.mode] .. (def.mode~="any" and (" ≤ " .. money(limit) .. "/шт.") or "") ..
    "; цель " .. rule.goal .. ", куплено " .. rule.bought .. ", осталось " .. self:Remaining(rule) ..
    "\n" .. (rule.enabled and (rule.reason or "В поиске") or "Пауза")
end
function U:SniperPanel()
  local parent=self.body
  local function pooled(kind,template) return self:Widget(kind,parent,template,"sniper") end
  local function b(caption,x,y,w,fn)
    local f=pooled("Button","UIPanelButtonTemplate"); f:Enable(); f:SetPoint("TOPLEFT",x,y); f:SetSize(w,24); f:SetText(caption); f:SetScript("OnClick",fn); return f
  end
  b("Добавить предмет",0,0,155,function() S:ShowEditor() end)
  b("Запустить все",163,0,145,function()
    if not C:MarketBindingMatches(C.db.current_market) then S:Notice("Подтвердите рынок"); return end
    for _,rule in ipairs(S:Rules()) do rule.enabled=S:Remaining(rule)>0 end; S:Notice("Поиск включён"); C:Refresh()
  end)
  b("Пауза",316,0,100,function() S:Pause(); C:Refresh() end)
  b("Импорт / экспорт",424,0,180,function() S:ShowTransfer() end)
  local status=pooled("FontString","GameFontHighlightSmall"); status:SetPoint("TOPLEFT",0,-32); status:SetWidth(630); status:SetJustifyH("LEFT")
  status:SetText("Поиск и сигналы не зависят от золота. Цена в правилах — за штуку."); self.sniperStatus=status
  b("Находки",0,-52,100,function() S:ShowFindings() end)
  b("Из выбранного задания",110,-52,220,function()
    local task=U:Task()
    if not task then S:Notice("Сначала выберите задание в «Возможностях»"); return end
    local p=panel(500,math.min(500,135+35*#task.lines))
    text(p,"Добавить цель из задания",18,-18,450)
    button(p,"Готовый предмет",18,-48,440,function() p:Hide(); S:ShowEditor(task.item_id) end)
    local scroll=CreateFrame("ScrollFrame",nil,p,"UIPanelScrollFrameTemplate"); scroll:SetPoint("TOPLEFT",18,-80); scroll:SetSize(435,math.min(340,35*#task.lines))
    local body=CreateFrame("Frame",nil,scroll); body:SetSize(420,35*#task.lines); scroll:SetScrollChild(body)
    for i,line in ipairs(task.lines) do
      button(body,C.Items:Label(line.item_id) .. " × " .. line.quantity,0,-35*(i-1),415,function()
        p:Hide(); S:ShowEditor(line.item_id,nil,{item_id=line.item_id,quantity=line.quantity,mode="any",priority="normal",discount_bps=5000,unit_limit_copper=0,use_bags=true})
      end)
    end
    button(p,"Закрыть",350,-p:GetHeight()+35,125,function() p:Hide() end)
  end)
  b("Журнал",340,-52,120,function() S:ShowHistory() end)
  b("Найти / рецепт",465,-52,145,function() S:ShowEditor() end)
  local scroll=self:Widget("ScrollFrame",parent,"UIPanelScrollFrameTemplate","sniper-list")
  scroll:SetPoint("TOPLEFT",0,-84); scroll:SetSize(625,math.max(100,parent:GetHeight()-90)); scroll:SetClipsChildren(true)
  local body=self:Widget("Frame",scroll,nil,"sniper-content"); local y=0
  self.sniperRows={}
  for _,rule in ipairs(S:Rules()) do
    local def=rule.definition
    local row=self:Widget("FontString",body,"GameFontHighlightSmall","sniper-row")
    row:SetPoint("TOPLEFT",0,y); row:SetWidth(385); row:SetJustifyH("LEFT")
    row:SetText(S:RowText(rule)); self.sniperRows[#self.sniperRows+1]={font=row,rule=rule}
    local function rowButton(caption,x,w,fn)
      local f=self:Widget("Button",body,"UIPanelButtonTemplate","sniper-row-button"); f:Enable(); f:SetPoint("TOPLEFT",x,y); f:SetSize(w,24); f:SetText(caption); f:SetScript("OnClick",fn)
    end
    rowButton(rule.enabled and "Пауза" or "Пуск",390,75,function() rule.enabled=not rule.enabled; if not rule.enabled then S:CancelSearch("Цель приостановлена") end; C:Refresh() end)
    rowButton("Правка",470,75,function() S:ShowEditor(def.item_id,rule) end)
    rowButton("×",550,35,function()
      if C.db.pending then S:Notice("Сначала завершите покупку"); return end
      if #C.db.sniper.events>=1900 then S:Notice("Сначала синхронизируйте журнал"); return end
      rule.enabled=false; rule.removed=true; S.alerts[def.id]=nil; S:Emit("rule_remove",{rule_id=def.id}); C:Refresh()
    end)
    local manual=self:Widget("Button",body,"UIPanelButtonTemplate","sniper-row-button"); manual:Enable(); manual:SetPoint("TOPLEFT",390,y-29); manual:SetSize(155,24); manual:SetText("Отметить купленное"); manual:SetScript("OnClick",function() S:ShowManualBought(def.id) end)
    y=y-80
  end
  body:SetSize(600,math.max(scroll:GetHeight(),-y)); self:AttachScrollChild(scroll,body)
end
