-- A private font with Cyrillic coverage, independent of the client's locale.
local _,C=...
local F={path="Interface\\AddOns\\CennikAddon\\Media\\DejaVuSans.ttf",styles={},owned={},sequence=0}
C.Fonts=F

function F:Set(target,size,flags)
  if not target or type(target.SetFont)~="function" then return end
  local oldSize,oldFlags
  if type(target.GetFont)=="function" then
    local _,height,options=target:GetFont();oldSize,oldFlags=height,options
  end
  size=type(size)=="number" and size or type(oldSize)=="number" and oldSize or 12
  flags=flags~=nil and flags or oldFlags or ""
  local ok,result=pcall(target.SetFont,target,self.path,size,flags)
  if ok and result~=false then return true end
  -- Report a missing/incomplete manual installation instead of hiding the cause.
  if not self.warned then
    self.warned=true
    print("Cennik: отсутствует шрифт. Скопируйте всю папку CennikAddon, включая Media, и перезапустите игру.")
  end
  for _,path in ipairs({"Fonts\\FRIZQT___CYR.TTF","Fonts\\ARIALN.TTF"}) do
    ok,result=pcall(target.SetFont,target,path,size,flags)
    if ok and result~=false then return true end
  end
end

function F:Style(source,size,flags)
  if type(source)=="string" then source=_G[source] end
  if not source or type(CreateFont)~="function" then return end
  if self.owned[source] and not size and flags==nil then return source end
  local styles=self.styles[source] or {};self.styles[source]=styles
  local key=tostring(size or "inherit") .. ":" .. tostring(flags or "inherit")
  if styles[key] then return styles[key] end
  self.sequence=self.sequence+1
  local font=CreateFont("CennikPrivateFont" .. self.sequence)
  self.owned[font]=true
  font:CopyFontObject(source)
  self:Set(font,size,flags)
  styles[key]=font
  return font
end

function F:Apply(widget)
  if not widget then return end
  -- Keep the source colors/shadows for normal, highlighted and disabled buttons.
  for _,state in ipairs({"Normal","Highlight","Disabled"}) do
    local getter,setter=widget["Get" .. state .. "FontObject"],widget["Set" .. state .. "FontObject"]
    if type(getter)=="function" and type(setter)=="function" then
      local source=getter(widget)
      local font=source and self:Style(source)
      if font then setter(widget,font) end
    end
  end
  if type(widget.GetFontString)=="function" then self:Set(widget:GetFontString()) end
  if type(widget.SetFont)=="function" then self:Set(widget) end
  if type(widget.Text)=="table" or type(widget.Text)=="userdata" then self:Set(widget.Text) end
end

function F:FontObject(widget,source)
  widget:SetFontObject(self:Style(source) or source)
  self:Apply(widget)
end

function F:CreateFontString(parent,...)
  local font=parent:CreateFontString(...)
  self:Set(font)
  return font
end

function F:CreateFrame(kind,...)
  local frame=CreateFrame(kind,...)
  if kind=="Button" or kind=="CheckButton" or kind=="EditBox" then self:Apply(frame) end
  return frame
end

function F:Tooltip()
  if self.tooltip then return self.tooltip end
  if not GameTooltip or type(CreateFont)~="function" then return GameTooltip end
  local tip=CreateFrame("GameTooltip","CennikTooltip",UIParent,"GameTooltipTemplate")
  self.tooltip=tip
  tip:HookScript("OnShow",function()
    for index=1,tip:NumLines() do
      F:Set(_G["CennikTooltipTextLeft" .. index])
      F:Set(_G["CennikTooltipTextRight" .. index])
    end
  end)
  return tip
end
