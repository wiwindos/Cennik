local _,C=...
local U={}
C.CollectorUI=U
local colors={error="|cffff7777",collect="|cff77ccff",ready="|cff77dd99",pause="|cffffcc66",wait="|cffdddddd"}
local menuItems={
  {id="production",label="Производство / крафт"},
  {id="professions",label="Обновить данные профессий"},
  {id="role",label="Роль персонажа"},
  {id="collector",label="Статус сбора и ошибки",collector=true},
  {id="diagnostics",label="Отчёт для поддержки",collector=true},
  {id="auction",label="Cennik на аукционе",auction=true},
  {id="sniper",label="Снайпер",auction=true,section="Sniper"},
  {id="bulk",label="Массовая проверка из веба",auction=true,section="Bulk"},
  {id="market",label="Подтвердить текущий рынок"},
  {id="automation",label="Автоцикл: статус"},
}
function U:MenuReason(item)
  if C.collectorOnly and not item.collector then return "В режиме сборщика доступны только сбор и диагностика" end
  if item.id=="role" and not (CennikImport and CennikImport.worker_discovery) then return "Выбор роли доступен после подключения через Worker" end
  if item.id=="market" and not C.db.current_market then return "Сначала выберите рынок в вебе и синхронизируйте аддон" end
  if item.auction and not C.Adapter:Open() then return "Откройте аукцион у NPC" end
end
function U:MenuAction(item)
  local reason=self:MenuReason(item)
  if reason then self.menuNote:SetText(reason);return false,reason end
  if item.auction then
    C.UI:Attach();C.UI:Show()
    if not C.UI.frame then self.menuNote:SetText(C.Adapter.error or "Не удалось открыть вкладку Cennik. Проверьте Auctionator");return false end
    if item.section then C.UI:SelectSection(item.section) else C.UI:SelectSection("Opportunities") end
  elseif item.id=="production" then C.Production:Show()
  elseif item.id=="professions" then C.Professions:Capture()
  elseif item.id=="role" then C.CharacterRole:Show()
  elseif item.id=="collector" then self:Show(false)
  elseif item.id=="diagnostics" then self:Show(true)
  elseif item.id=="automation" then
    if C.Automation and C.Automation.config and not C.Automation.db.local_pause then C.Automation:Stop() end
    self:Show(false)
  elseif item.id=="market" then
    local ok,message=C:BindMarket(C.db.current_market)
    self.menuNote:SetText(ok and "Рынок подтверждён для этого персонажа" or message)
    return ok,message
  end
  self.menu:Hide()
  return true
end
function U:RefreshMenu(resetHint)
  if not self.menu or not self.menu:IsShown() then return end
  local count=0
  for index,item in ipairs(menuItems) do
    local b=self.menuButtons[index]
    b:SetText(item.id=="automation" and C.Automation and C.Automation.config and not C.Automation.db.local_pause and "Остановить автоцикл" or item.label)
    if not C.collectorOnly or item.collector then
      count=count+1;b:ClearAllPoints();b:SetPoint("TOPLEFT",16,-38-(count-1)*32)
      b:SetEnabled(not self:MenuReason(item));b:Show()
    else b:Hide() end
  end
  self.menu:SetHeight(48+count*32+65)
  if resetHint then
    self.menuNote:SetText(C.collectorOnly and "Управление сбором и отчёт для поддержки доступны без аукциона."
      or C.Adapter:Open() and "Производство, роль и диагностика доступны в любом месте."
      or "Закупка, снайпер и массовая проверка доступны при открытом аукционе.")
  end
end
function U:ToggleMenu()
  if self.menu and self.menu:IsShown() then self.menu:Hide();return end
  if not self.menu then
    local w=CreateFrame("Frame","CennikAccessMenu",UIParent,"BasicFrameTemplateWithInset");self.menu=w
    w:SetSize(310,370);w:SetPoint("TOPRIGHT",self.access,"BOTTOMRIGHT",0,-4);w:SetFrameStrata("DIALOG");w:SetClampedToScreen(true)
    local title=w:CreateFontString(nil,"OVERLAY","GameFontNormal");title:SetPoint("TOPLEFT",16,-13);title:SetText("Cennik · меню")
    self.menuNote=w:CreateFontString(nil,"OVERLAY","GameFontHighlightSmall");self.menuNote:SetPoint("BOTTOMLEFT",16,15);self.menuNote:SetSize(278,55);self.menuNote:SetJustifyH("LEFT")
    self.menuButtons={}
    for index,item in ipairs(menuItems) do
      local b=CreateFrame("Button",nil,w,"UIPanelButtonTemplate");self.menuButtons[index]=b
      b:SetSize(278,27);b:SetText(item.label)
      if b.SetMotionScriptsWhileDisabled then b:SetMotionScriptsWhileDisabled(true) end
      b:SetScript("OnClick",function() U:MenuAction(item) end)
      b:SetScript("OnEnter",function() U.menuNote:SetText(U:MenuReason(item) or (item.auction and "Открыть вкладку Cennik на аукционе" or "Доступно без открытого аукциона")) end)
    end
    w:SetScript("OnShow",function() U:RefreshMenu(true) end)
    if UISpecialFrames then table.insert(UISpecialFrames,"CennikAccessMenu") end
  end
  self.menu:Show();self:RefreshMenu(true)
end
function U:Initialize()
  if not self.access then
    local b=CreateFrame("Button",nil,Minimap or UIParent,"UIPanelButtonTemplate");self.access=b
    b:SetSize(85,24);b:SetPoint("TOPRIGHT",Minimap or UIParent,"BOTTOMRIGHT",0,-8);b:SetText("Cennik")
    b:SetScript("OnClick",function() U:ToggleMenu() end)
    b:SetScript("OnEnter",function()
      if not GameTooltip then return end
      GameTooltip:SetOwner(b,"ANCHOR_LEFT");GameTooltip:SetText("Cennik · меню команд")
      local current=C.CollectorStatus.db and C.CollectorStatus.db.current
      GameTooltip:AddLine(current and current.message or "Производство, роль персонажа, статус и диагностика",1,1,1,true);GameTooltip:Show()
    end)
    b:SetScript("OnLeave",function() if GameTooltip then GameTooltip:Hide() end end)
    if AuctionHouseFrame then
      local badge=CreateFrame("Button",nil,AuctionHouseFrame,"UIPanelButtonTemplate");self.badge=badge
      badge:SetSize(420,25);badge:SetPoint("TOPRIGHT",AuctionHouseFrame,"BOTTOMRIGHT",0,-3)
      badge:SetScript("OnClick",function() U:Show() end)
    end
  end
  self:Refresh()
end
function U:Show(report)
  if not self.window then
    local w=CreateFrame("Frame","CennikCollectorWindow",UIParent,"BasicFrameTemplateWithInset");self.window=w
    w:SetSize(650,515);w:SetPoint("CENTER");w:SetFrameStrata("DIALOG")
    local title=w:CreateFontString(nil,"OVERLAY","GameFontNormal");title:SetPoint("TOPLEFT",15,-12);title:SetText("Cennik · сбор данных и диагностика")
    local scroll=CreateFrame("ScrollFrame",nil,w,"UIPanelScrollFrameTemplate");scroll:SetPoint("TOPLEFT",18,-45);scroll:SetSize(590,340)
    local edit=CreateFrame("EditBox",nil,scroll);self.edit=edit
    edit:SetMultiLine(true);edit:SetAutoFocus(false);edit:SetFontObject("ChatFontNormal");edit:SetWidth(580);edit:SetHeight(550)
    edit:SetScript("OnEscapePressed",function() w:Hide() end);scroll:SetScrollChild(edit)
    local function button(text,x,width,fn)
      local b=CreateFrame("Button",nil,w,"UIPanelButtonTemplate");b:SetSize(width,26);b:SetPoint("BOTTOMLEFT",x,40);b:SetText(text);b:SetScript("OnClick",fn);return b
    end
    button("Состояние и подробности",18,200,function() U:Show(false) end)
    button("Отчёт для поддержки",230,185,function() U:Show(true) end)
    self.toggle=button("",430,195,function()
      C.CollectorStatus.db.paused=C.CollectorStatus:IsEnabled() and true or false
      C.db.collector.enabled=not C.CollectorStatus.db.paused
      if C.CollectorStatus.db.paused then C.CollectorStatus:Set("DISABLED") end
      U:Refresh()
    end)
    local function propose(label,x,kind)
      local b=CreateFrame("Button",nil,w,"UIPanelButtonTemplate");b:SetSize(295,26);b:SetPoint("BOTTOMLEFT",x,75);b:SetText(label)
      b:SetScript("OnClick",function() C.AuctionContext:Propose(kind);U:Show(false) end)
    end
    propose("Предложить: обычный аукцион",18,"ordinary")
    propose("Предложить: межфракционный",325,"neutral")
    local note=w:CreateFontString(nil,"OVERLAY","GameFontHighlightSmall");note:SetPoint("BOTTOMLEFT",18,15);note:SetWidth(610)
    note:SetText("Отчёт: Ctrl+C → отправьте разработчику. Запись данных игрой: /reload или штатный выход.")
  end
  self.report=report==true;self.report_loaded=false;self.window:Show();self:Refresh()
  if self.report then self.edit:SetFocus();self.edit:HighlightText() end
end
function U:Refresh()
  local s=C.CollectorStatus;local d=s and s.db;if not d then return end
  local current=d.current or {};local color=colors[current.level] or colors.wait
  if self.badge then self.badge:SetText(color .. "Cennik · " .. (current.message or "сбор данных") .. "|r") end
  if self.access then self.access:SetText(color .. "Cennik|r") end
  self:RefreshMenu()
  if self.window and self.window:IsShown() then
    if not self.report then self.edit:SetText(s:Summary()) end
    if self.report and not self.report_loaded then self.edit:SetText(s:Report());self.report_loaded=true end
    local text=self.edit:GetText();local _,breaks=text:gsub("\n","")
    self.edit:SetHeight(math.max(550,(breaks+math.ceil(#text/70)+2)*16))
    self.toggle:SetText(s:IsEnabled() and "Приостановить сбор" or "Включить сбор")
  end
end
