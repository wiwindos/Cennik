local _, C = ...
local G={elapsed=0}
C.IdleGuard=G
function G:Ready()
  local role=CennikImport and CennikImport.production and CennikImport.production.client.role
  if not C.db or not C.db.idle_probe_enabled or role~="auctioneer" or not C.Adapter:Open() then return false end
  if (InCombatLockdown and InCombatLockdown()) or (UnitAffectingCombat and UnitAffectingCombat("player")) then return false end
  if GetCurrentKeyBoardFocus and GetCurrentKeyBoardFocus() then return false end
  if C.db.pending or (C.BuyQueue and C.BuyQueue.ticket) or C.Scanner.active or C.Bulk.active then return false end
  if C.MarketCollector.requested_at or C.MarketCollector.packing or C.Adapter.shopping or C.Adapter.fullScan then return false end
  if C.Sniper and (C.Sniper.current or C.Sniper.ticket) then return false end
  return C.AuctionContext:Check("collect")
end
function G:Update()
  if not self.frame then return end
  local ready=self:Ready()
  local colors=ready and {{17,223,109},{219,81,247},{31,147,251}} or {{223,17,109},{219,81,247},{31,147,251}}
  for i,t in ipairs(self.textures) do local c=colors[i];t:SetColorTexture(c[1]/255,c[2]/255,c[3]/255,1) end
  if C.db and C.db.idle_probe_enabled then self.frame:Show() else self.frame:Hide() end
end
function G:Initialize()
  if not C.db or not C.db.idle_probe_enabled then return end
  if self.frame then return end
  self.frame=CreateFrame("Frame",nil,UIParent)
  self.frame:SetSize(24,8);self.frame:SetPoint("TOPLEFT",UIParent,"TOPLEFT",0,0);self.frame:SetFrameStrata("TOOLTIP")
  if self.frame.SetIgnoreParentScale then self.frame:SetIgnoreParentScale(true) end
  self.frame:SetScale(1);self.textures={}
  for i=1,3 do local t=self.frame:CreateTexture(nil,"OVERLAY");t:SetSize(8,8);t:SetPoint("TOPLEFT",(i-1)*8,0);self.textures[i]=t end
  self.frame:SetScript("OnUpdate",function(_,elapsed) G.elapsed=G.elapsed+elapsed;if G.elapsed>.2 then G.elapsed=0;G:Update() end end)
  self:Update()
end
