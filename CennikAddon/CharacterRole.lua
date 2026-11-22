local _, C = ...
local R={}
C.CharacterRole=R
function R:Capture(role)
  if not C.db or not (CennikImport and CennikImport.worker_discovery) then return end
  local realm,name,faction=C:PlayerIdentity()
  if not realm or realm=="" then return end
  C.db.character_profiles=C.db.character_profiles or {}
  local key=realm .. ":" .. name
  local previous=C.db.character_profiles[key] or {}
  local market=C.db.server_binding and C.db.server_binding.market or C.db.current_market
  if previous.market and previous.market~=market then previous={} end
  local client=CennikImport.production and CennikImport.production.client
  local assigned=client and client.role
  if assigned~="auctioneer" and assigned~="crafter" then assigned="unassigned" end
  local previousAt=previous.role_chosen_at or 0
  local assignedAt=client and client.role_chosen_at or 0
  local chosen=role or (assignedAt>previousAt and assigned) or previous.role or assigned or "unassigned"
  local chosenAt=role and math.max(previousAt+1,math.floor(C:Now()*1000+(GetTime()%1)*1000)) or math.max(previousAt,assignedAt)
  local professions=previous.professions or {}
  if GetTradeSkillLine then
    local name=GetTradeSkillLine()
    if type(name)=="string" and name~="" and name~="UNKNOWN" then
      local found=false;for _,p in ipairs(professions) do if p==name then found=true end end
      if not found and #professions<20 then professions[#professions+1]=name end
    end
  elseif C_TradeSkillUI and C_TradeSkillUI.GetBaseProfessionInfo then
    local ok,info=pcall(C_TradeSkillUI.GetBaseProfessionInfo)
    local name=ok and info and info.professionName
    if type(name)=="string" and name~="" then
      local found=false;for _,p in ipairs(professions) do if p==name then found=true end end
      if not found and #professions<20 then professions[#professions+1]=name end
    end
  end
  C.db.character_profiles[key]={character=name,realm=realm,
    faction=faction,role=chosen,role_chosen_at=chosenAt,professions=professions,
    market=market,game_version=GetBuildInfo and GetBuildInfo() or nil}
  return chosen
end
function R:Choose(role)
  if role~="auctioneer" and role~="crafter" then return false end
  if C.db.pending then print("Cennik: сначала уточните результат покупки, затем меняйте роль");return false end
  if not self:Capture(role) then print("Cennik: сначала подтвердите выбранный рынок кнопкой «Подтвердить рынок» на вкладке Cennik");return false end
  print("Cennik: выбрана роль " .. (role=="auctioneer" and "Аукционер" or "Крафтер") .. ". Выполните /reload, дождитесь Worker и повторите /reload для получения заданий.")
  C:Refresh()
  if self.frame then self.frame:Hide() end
  return true
end
function R:Show()
  if not (CennikImport and CennikImport.worker_discovery) then return end
  if not self.frame then
    local frame=C.Fonts:CreateFrame("Frame",nil,UIParent,"BasicFrameTemplateWithInset")
    frame:SetSize(360,150);frame:SetPoint("CENTER");frame:SetFrameStrata("DIALOG")
    local title=C.Fonts:CreateFontString(frame,nil,"OVERLAY","GameFontNormal")
    title:SetPoint("TOP",0,-30);title:SetText("Cennik: роль этого персонажа")
    for index,spec in ipairs({{"auctioneer","Аукционер"},{"crafter","Крафтер"}}) do
      local role=spec[1]
      local button=C.Fonts:CreateFrame("Button",nil,frame,"UIPanelButtonTemplate")
      button:SetSize(145,28);button:SetPoint("TOPLEFT",20+(index-1)*170,-70);button:SetText(spec[2])
      button:SetScript("OnClick",function() R:Choose(role) end)
    end
    self.frame=frame
  end
  self.frame:Show()
end
function R:Initialize()
  local role=self:Capture()
  if role=="unassigned" and not (CennikImport and CennikImport.worker_automatic) then C_Timer.After(2,function() R:Show() end) end
end
local frame=C.Fonts:CreateFrame("Frame")
frame:RegisterEvent("TRADE_SKILL_SHOW")
frame:SetScript("OnEvent",function() R:Capture() end)
