local _,C=...
local U={}
C.CollectorUI=U
local colors={error="|cffff7777",collect="|cff77ccff",ready="|cff77dd99",pause="|cffffcc66",wait="|cffdddddd"}
function U:Initialize()
  if not self.access then
    local b=CreateFrame("Button",nil,Minimap or UIParent,"UIPanelButtonTemplate");self.access=b
    b:SetSize(75,24);b:SetPoint("TOPRIGHT",Minimap or UIParent,"BOTTOMRIGHT",0,-8);b:SetText("Сбор: Cennik")
    b:SetScript("OnClick",function() U:Show() end)
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
    w:SetSize(650,470);w:SetPoint("CENTER");w:SetFrameStrata("DIALOG")
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
  if self.access then self.access:SetText(color .. "Сбор: Cennik|r") end
  if self.window and self.window:IsShown() then
    if not self.report then self.edit:SetText(s:Summary()) end
    if self.report and not self.report_loaded then self.edit:SetText(s:Report());self.report_loaded=true end
    local text=self.edit:GetText();local _,breaks=text:gsub("\n","")
    self.edit:SetHeight(math.max(550,(breaks+math.ceil(#text/70)+2)*16))
    self.toggle:SetText(s:IsEnabled() and "Приостановить сбор" or "Включить сбор")
  end
end
