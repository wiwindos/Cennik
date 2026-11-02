local _,C=...
local A={elapsed=0}
C.Automation=A
local names={[0]="выключен",[1]="ожидаем завершения операции",[2]="сбор / снайпер работают",[3]="готов к сохранению",
  [4]="ожидаем взаимодействия с NPC",[5]="открытие аукциона вручную",[6]="заблокирован"}
local function npcID(unit)
  local guid=UnitGUID and UnitGUID(unit)
  if type(guid)~="string" or (issecretvalue and issecretvalue(guid)) then return end
  local fields={};for part in guid:gmatch("[^-]+") do fields[#fields+1]=part end
  if fields[1]=="Creature" then return tonumber(fields[6]) end
end
function A:Initialize()
  self.config=nil;self.quiescing=false
  local config=CennikImport and CennikImport.automation
  local client=CennikImport and CennikImport.production and CennikImport.production.client
  if not config or not config.enabled or C.collectorOnly or not client or client.role~="auctioneer"
    or config.client_id~=C.db.client_id or type(config.token)~="string" or #config.token~=12 or not config.token:match("^%x+$") then
    if self.marker then self.marker:Hide() end;return
  end
  self.config=config
  C.db.auto_cycle=C.db.auto_cycle or {}
  self.db=C.db.auto_cycle
  if self.db.token~=config.token then
    self.db.token=config.token;self.db.local_pause=false;self.db.resume_pending=false;self.db.npc_id=nil
    self.db.last_flush=C:Now();self.db.resume_rules={}
  end
  self.db.boot=(self.db.boot or 0)+1
  self.loaded=GetTime();self.elapsed=0
  if not self.marker then
    local m=CreateFrame("Frame",nil,UIParent);self.marker=m
    m:SetSize(300,32);m:SetFrameStrata("TOOLTIP");m:EnableMouse(false)
    m:SetPoint("TOPLEFT",UIParent,"TOPLEFT",12,-12)
    if UIParent and UIParent.GetEffectiveScale then m:SetScale(1/UIParent:GetEffectiveScale()) end
    self.cells={}
    for i=1,7 do
      local t=m:CreateTexture(nil,"OVERLAY");self.cells[i]=t;t:SetSize(10,10);t:SetPoint("TOPLEFT",(i-1)*10,0)
    end
    self.label=m:CreateFontString(nil,"OVERLAY","GameFontHighlightSmall");self.label:SetPoint("TOPLEFT",0,-13);self.label:SetWidth(300);self.label:SetJustifyH("LEFT")
  end
  self.marker:Show();self:Tick(.25)
end
function A:Stop()
  if not self.db then return end
  self.db.local_pause=true;self.db.resume_pending=false;self.quiescing=false
  self.reason="Остановлен в игре. Для включения сохраните настройки Worker и выполните /reload"
  self:Paint(0)
end
function A:Summary()
  if not self.config then return "Автоцикл: выключен" end
  return "Автоцикл: " .. (names[self.code or 0] or "ожидание") .. " · " .. (self.reason or "")
end
function A:Capture()
  if not C.db.sniper then return end
  self.db.resume_rules={}
  for id,rule in pairs(C.db.sniper.rules) do if rule.enabled and not rule.removed then self.db.resume_rules[id]=true end end
  self.db.resume_collect=C.db.collector and C.db.collector.enabled==true
end
function A:Busy()
  if C.db.pending then return true,"Результат покупки ещё не определён; перезагрузка заблокирована" end
  if InCombatLockdown and InCombatLockdown() then return true,"Бой: перезагрузка заблокирована" end
  if GetCurrentKeyBoardFocus and GetCurrentKeyBoardFocus() then return true,"Открыт ввод текста" end
  if C.MarketCollector.requested_at or C.MarketCollector.packing or C.Adapter.fullScan or C.Adapter.shopping
    or C.Scanner.active or (C.Bulk and C.Bulk.active) then return true,"Скан или другая операция ещё выполняются" end
end
function A:Paint(code)
  self.code=code
  local token=self.config.token
  local function color(t,r,g,b) t:SetColorTexture(r/255,g/255,b/255,1) end
  color(self.cells[1],219,51,177)
  for i=1,2 do local offset=(i-1)*6;color(self.cells[i+1],tonumber(token:sub(offset+1,offset+2),16),tonumber(token:sub(offset+3,offset+4),16),tonumber(token:sub(offset+5,offset+6),16)) end
  local r,g,b=32+code*24,16+(math.floor(GetTime())%16)*14,16+(self.db.boot%16)*14
  color(self.cells[4],r,g,b);color(self.cells[5],255-r,255-g,255-b)
  color(self.cells[6],41,211,89);color(self.cells[7],16,32,48)
  self.label:SetText("Cennik авто: " .. (names[code] or "ожидание"))
end
function A:Prepare()
  self:Capture();self.quiescing=true;self.ready_until=GetTime()+15
  if C.Sniper then C.Sniper:CancelSearch("Подготовка безопасного сохранения") end
  C.BuyQueue.ticket=nil
end
function A:Restore()
  if not self.db.resume_pending then return true end
  local ok=C.AuctionContext:Check("buy");if not ok then return false end
  local context=C.AuctionContext:Snapshot()
  if not context or context.npc_id~=self.db.npc_id then self.reason="Открылся другой NPC; восстановление заблокировано";return false end
  C.UI:Attach();C.UI:Show()
  local any=false
  for id,enabled in pairs(self.db.resume_rules or {}) do
    local rule=C.db.sniper and C.db.sniper.rules[id]
    if enabled and rule and not rule.removed and C.Sniper:Remaining(rule)>0 then rule.enabled=true;any=true end
  end
  if any then
    C.UI:SelectSection("Sniper");C.Sniper.manualHold=nil;C.Sniper.foreignTab=false
  else C.UI:SelectSection("Opportunities") end
  if self.db.resume_collect and C.db.collector then C.db.collector.enabled=true end
  self.db.resume_pending=false
  self.reason="Аукцион открыт; восстановлены прежние активные цели и режим сбора"
  return true
end
function A:Tick(elapsed)
  if not self.config or not self.db then return end
  self.elapsed=self.elapsed+elapsed;if self.elapsed<.2 then return end;self.elapsed=0
  if self.db.local_pause then self:Paint(0);return end
  if GetTime()-self.loaded<3 then self.reason="Загрузка интерфейса";self:Paint(1);return end
  if not C:MarketBindingMatches(C.db.current_market) then self.reason="Подтвердите рынок персонажа";self:Paint(6);return end
  local busy,reason=self:Busy()
  if busy then self.reason=reason;self:Paint(C.db.pending and 6 or 1);return end
  if not C.Adapter:Open() then
    self.quiescing=false
    if self.db.resume_pending then
      if not self.db.npc_id or npcID("target")~=self.db.npc_id then self.reason="Выберите прежнего NPC-аукциониста целью и подойдите к нему";self:Paint(6);return end
      self.reason="Worker должен открыть выбранного NPC";self:Paint(4)
    else self.reason="Первое открытие аукциона — вручную";self:Paint(5) end
    return
  end
  local ok,context=C.AuctionContext:Check("buy")
  if not ok then self.reason=context;self:Paint(6);return end
  if self.db.resume_pending and not self:Restore() then self:Paint(6);return end
  self.db.npc_id=context.npc_id
  if self.quiescing then
    if GetTime()<self.ready_until then self.reason="Готов к /reload; Worker ждёт живой индикатор";self:Paint(3);return end
    self.quiescing=false;self.retry_after=GetTime()+20
    local latest=C.db.collector and C.db.collector.last_scan
    self.skipped_scan_id=latest and latest.scan_id
    self.defer_flush_until=C:Now()+math.max(300,tonumber(self.config.flush_seconds) or 900)
  end
  self:Capture()
  local last=C.db.collector and C.db.collector.last_scan
  local fresh=last and last.full and not last.saved and last.scan_id~=self.db.flushed_scan_id and last.scan_id~=self.skipped_scan_id
  local due=C:Now()>=(self.defer_flush_until or 0) and C:Now()-(self.db.last_flush or C:Now())>=math.max(300,tonumber(self.config.flush_seconds) or 900)
  if (fresh or due) and GetTime()>=(self.retry_after or 0) then
    if npcID("target")~=self.db.npc_id then self.reason="Перед перезагрузкой выберите текущего аукциониста целью";self:Paint(6);return end
    self:Prepare();self.reason="Полный скан готов; безопасная пауза";self:Paint(3)
  else self.reason="Ожидаем полный скан или срок сохранения";self:Paint(2) end
end
local frame=CreateFrame("Frame")
frame:RegisterEvent("PLAYER_LOGOUT")
frame:SetScript("OnUpdate",function(_,elapsed) A:Tick(elapsed) end)
frame:SetScript("OnEvent",function()
  if not A.config or not A.db or A.db.local_pause or C.db.pending then return end
  if not A.quiescing then A:Capture() end
  A.db.resume_pending=(A.quiescing or C.Adapter:Open()) and A.db.npc_id~=nil
  A.db.last_flush=C:Now()
  local latest=C.db.collector and C.db.collector.last_scan
  A.db.flushed_scan_id=latest and latest.scan_id
end)
