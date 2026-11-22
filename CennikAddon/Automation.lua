local _,C=...
local A={elapsed=0}
C.Automation=A
local names={[0]="выключен",[1]="ожидаем завершения операции",[2]="сбор / снайпер работают",[3]="готов к сохранению",
  [4]="ожидаем взаимодействия с NPC",[5]="открытие аукциона вручную",[6]="заблокирован"}
function A:Initialize()
  self.config=nil;self.quiescing=false
  local config=CennikImport and CennikImport.automation
  local client=CennikImport and CennikImport.production and CennikImport.production.client
  if not config or not config.enabled or C.collectorOnly
    or (not config.any_character and (not client or client.role~="auctioneer" or config.client_id~=C.db.client_id))
    or type(config.token)~="string" or #config.token~=12 or not config.token:match("^%x+$") then
    if self.marker then self.marker:Hide() end;return
  end
  self.config=config
  C.db.auto_cycle=C.db.auto_cycle or {}
  self.db=C.db.auto_cycle
  local character=C:RealmName() .. ":" .. C:CharacterName()
  self.characterKey=5381;for i=1,#character do self.characterKey=(self.characterKey*33+character:byte(i))%4294967296 end
  if self.db.token~=config.token or (config.any_character and (self.db.character~=character or self.db.start_revision~=config.start_revision)) then
    self.db.token=config.token;self.db.local_pause=false;self.db.resume_pending=false;self.db.npc_id=nil
    self.db.last_flush=C:Now();self.db.resume_rules={}
  end
  self.db.character=character
  self.db.start_revision=config.start_revision
  local searchToken=config.token .. tostring(config.start_revision or "") .. character
  if config.any_character and config.auto_sniper then
    for _,rule in pairs(C.db.sniper and C.db.sniper.rules or {}) do
      if rule.auto_started_token~=searchToken then
        if not rule.removed and C.Sniper:Remaining(rule)>0 then rule.enabled=true end
        rule.auto_started_token=searchToken
      end
    end
  end
  self.initial_open=not self.db.resume_pending
  local collectorToken=config.token .. tostring(config.start_revision or "")
  if self.db.collector_auto_token~=collectorToken then
    self.db.collector_auto_token=collectorToken
    if config.auto_scan~=false and C.db.collector then C.MarketCollector:SetAutoScan(true) end
  end
  self.db.boot=(self.db.boot or 0)+1
  self.loaded=GetTime();self.elapsed=0
  if not self.marker then
    local m=C.Fonts:CreateFrame("Frame",nil,UIParent);self.marker=m
    m:SetSize(380,60);m:SetFrameStrata("TOOLTIP");m:EnableMouse(false)
    m:SetPoint("TOPLEFT",UIParent,"TOPLEFT",12,-12)
    if UIParent and UIParent.GetEffectiveScale then m:SetScale(1/UIParent:GetEffectiveScale()) end
    self.cells={}
    for i=1,10 do
      local t=m:CreateTexture(nil,"OVERLAY");self.cells[i]=t;t:SetSize(10,10);t:SetPoint("TOPLEFT",(i-1)*10,0)
    end
    self.label=C.Fonts:CreateFontString(m,nil,"OVERLAY","GameFontHighlightSmall");self.label:SetPoint("TOPLEFT",0,-13);self.label:SetSize(380,42);self.label:SetJustifyH("LEFT");self.label:SetJustifyV("TOP")
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
function A:IsActive()
  return self.config and self.db and not self.db.local_pause
end
function A:ScanCapacity()
  local m=C.MarketCollector
  return #(C.db.market_scans or {})<m.maxQueue and m:QueuedBytes()+m.maxBytes<=m.maxTotalBytes
end
function A:ScanDue()
  local state=C.db.collector
  return self:IsActive() and state and state.enabled and C.Adapter:Open()
    and not (C.CollectorStatus and C.CollectorStatus.db and C.CollectorStatus.db.paused)
    and not self.quiescing and not C.MarketCollector.requested_at and not C.MarketCollector.packing
    and not C.db.pending and not C.Scanner.active and not (C.Bulk and C.Bulk.active)
    and not (GetCurrentKeyBoardFocus and GetCurrentKeyBoardFocus()) and not (InCombatLockdown and InCombatLockdown())
    and not C.Adapter.shopping and not C.Adapter.fullScan
    and not (C.Sniper and (C.Sniper.manualHold or C.Sniper.foreignTab))
    and C:Now()>=(state.next_at or 0) and self:ScanCapacity()
end
function A:SaveDue()
  if not self:IsActive() or not C.Adapter:Open() or GetTime()<(self.retry_after or 0) then return false end
  local last=C.db.collector and C.db.collector.last_scan
  local fresh=last and last.full and not last.saved and last.scan_id~=self.db.flushed_scan_id
  local due=C:Now()>=(self.defer_flush_until or 0) and C:Now()-(self.db.last_flush or C:Now())>=math.max(300,tonumber(self.config.flush_seconds) or 900)
  return fresh or due
end
function A:CaptureUI()
  local s=C.Sniper
  if not s then return end
  self.db.resume_overlay_hidden=s.overlayHidden==true
  self.db.resume_overlay_requested=s.overlayRequested==true or (s.overlay and s.overlay:IsShown()) or false
  self.db.resume_selected=s.selected
end
function A:Capture()
  if not C.db.sniper then return end
  self.db.resume_rules={};self.db.resume_definitions={}
  for id,rule in pairs(C.db.sniper.rules) do if rule.enabled and not rule.removed then
    self.db.resume_rules[id]=true;self.db.resume_definitions[id]=C.Sniper:DefinitionText(rule.definition)
  end end
  self.db.resume_collect=C.db.collector and C.db.collector.enabled==true
  self:CaptureUI()
end
function A:Busy(initial)
  if C.db.pending then return true,"Результат покупки ещё не определён; перезагрузка заблокирована" end
  if InCombatLockdown and InCombatLockdown() then return true,"Бой: перезагрузка заблокирована" end
  if GetCurrentKeyBoardFocus and GetCurrentKeyBoardFocus() then return true,"Открыт ввод текста" end
  if C.Sniper and (C.Sniper.manualHold or C.Sniper.foreignTab)
    and not ((initial or self.db.resume_pending) and (C.Sniper.manualHoldSource=="tab" or C.Sniper.manualHoldSource=="scan")) then
    return true,"Ручной поиск AH: нажмите «Продолжить» в Cennik"
  end
  if C.MarketCollector.packing then
    local p=C.MarketCollector.packing
    return true,"Обрабатываем полный скан: " .. tostring(p.index or 0) .. "/" .. tostring(p.count or 0) .. " строк"
  end
  if C.MarketCollector.requested_at then
    if C.MarketCollector.response_received_at then return true,"Сервер ответил; Auctionator обрабатывает лоты (" .. math.max(0,math.floor(C:Now()-C.MarketCollector.response_received_at)) .. " с)" end
    return true,"Получаем полный скан: ждём ответ сервера (" .. math.max(0,math.floor(C:Now()-C.MarketCollector.requested_at)) .. " с)"
  end
  if C.Adapter.fullScan then return true,"Полный скан Auctionator ещё не завершён" end
  if C.Adapter.shopping then return true,"Поиск Auctionator ещё выполняется" end
  if C.Scanner.active then return true,"Проверка лотов Cennik ещё выполняется" end
  if C.Bulk and C.Bulk.active then return true,"Массовая проверка из веба ещё выполняется" end
  if C.Sniper and C.Sniper.current then return true,"Запрос снайпера: ожидаем ответ аукциона" end
end
function A:InitialOpen()
  if (not self.initial_open and C.Sniper.manualHoldSource~="scan") or self.db.resume_pending or not C.Adapter:Open() then return end
  if self:Busy(true) then return end
  local ok,context=C.AuctionContext:Check("buy")
  if not ok then return end
  if C.Sniper:ResumeSearch() then
    self.initial_open=false;self.db.npc_id=context.npc_id
    if C.Sniper.RenderOverlay then C.Sniper:RenderOverlay() end
  end
end
function A:Paint(code)
  self.code=code
  local token=self.config.token
  local function color(t,r,g,b) t:SetColorTexture(r/255,g/255,b/255,1) end
  color(self.cells[1],219,51,177)
  for i=1,2 do local offset=(i-1)*6;color(self.cells[i+1],tonumber(token:sub(offset+1,offset+2),16),tonumber(token:sub(offset+3,offset+4),16),tonumber(token:sub(offset+5,offset+6),16)) end
  local r,g,b=32+code*24,16+(math.floor(GetTime())%16)*14,16+(self.db.boot%16)*14
  color(self.cells[4],r,g,b);color(self.cells[5],255-r,255-g,255-b)
  color(self.cells[6],41,211,89);color(self.cells[7],16,32,self.config.any_character and 80 or 48)
  if self.config.any_character then
    local digits={};local value=self.characterKey;local checksum=0
    for i=8,1,-1 do digits[i]=value%16;checksum=checksum+digits[i];value=math.floor(value/16) end
    digits[9]=checksum%16
    for i=1,3 do local n=(i-1)*3;color(self.cells[7+i],16+14*digits[n+1],16+14*digits[n+2],16+14*digits[n+3]) end
  end
  self.label:SetText("Cennik авто: " .. (self.reason or names[code] or "ожидание"))
end
function A:Prepare()
  self:Capture();self.quiescing=true;self.ready_until=GetTime()+120
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
    local definition=(self.db.resume_definitions or {})[id]
    if enabled and rule and definition==C.Sniper:DefinitionText(rule.definition) and not rule.removed and C.Sniper:Remaining(rule)>0 then rule.enabled=true;any=true end
  end
  if any then
    C.UI:SelectSection("Sniper")
  else C.UI:SelectSection("Opportunities") end
  C.Sniper.manualHold=nil;C.Sniper.manualHoldSource=nil;C.Sniper.foreignTab=false
  self.initial_open=false
  if self.db.resume_collect and C.db.collector then C.db.collector.enabled=true end
  C.Sniper.overlayHidden=self.db.resume_overlay_hidden==true
  C.Sniper.overlayRequested=self.db.resume_overlay_requested==true
  if self.db.resume_selected and C.db.sniper.rules[self.db.resume_selected] then C.Sniper.selected=self.db.resume_selected end
  if C.Sniper.RenderOverlay then C.Sniper:RenderOverlay() end
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
  if self.config.any_character and not self.config.client_id then
    if C.db.pending then self.reason="Уточните прежнюю покупку перед сменой персонажа";self:Paint(6);return end
    local busy,reason=self:Busy(true)
    if busy then self.reason=reason;self:Paint(1);return end
    self.reason="Worker подключает текущего персонажа; требуется сохранение /reload";self:Paint(7);return
  end
  self:InitialOpen()
  local busy,reason=self:Busy()
  if busy then
    if self.quiescing then self.quiescing=false;self.retry_after=GetTime()+5 end
    self.reason=reason;self:Paint(C.db.pending and 6 or 1);return
  end
  if not C.Adapter:Open() then
    self.quiescing=false
    -- The interact binding may select the nearest NPC itself. Validate after opening.
    self.reason="Worker должен нажать взаимодействие; тип аукциона и NPC проверяются после открытия";self:Paint(4)
    return
  end
  local ok,context=C.AuctionContext:Check("buy")
  if not ok then self.reason=context;self:Paint(6);return end
  if self.db.resume_pending and not self:Restore() then self:Paint(6);return end
  self.db.npc_id=context.npc_id
  if self.quiescing then
    if GetTime()<self.ready_until then self.reason="Готов к /reload; Worker ждёт живой индикатор";self:Paint(3);return end
    self.quiescing=false;self.retry_after=GetTime()+20
    self.defer_flush_until=C:Now()+math.max(300,tonumber(self.config.flush_seconds) or 900)
  end
  self:Capture()
  if self:SaveDue() then
    self:Prepare();self.reason="Полный скан готов; безопасная пауза";self:Paint(3)
  else self.reason="Ожидаем полный скан или срок сохранения";self:Paint(2) end
end
local frame=C.Fonts:CreateFrame("Frame")
frame:RegisterEvent("PLAYER_LOGOUT")
frame:SetScript("OnUpdate",function(_,elapsed) A:Tick(elapsed) end)
frame:SetScript("OnEvent",function()
  if not A.config or not A.db or A.db.local_pause or C.db.pending then return end
  if not A.quiescing then A:Capture() end
  A:CaptureUI()
  A.db.resume_pending=(A.quiescing or C.Adapter:Open()) and A.db.npc_id~=nil
  A.db.last_flush=C:Now()
  local latest=C.db.collector and C.db.collector.last_scan
  A.db.flushed_scan_id=latest and latest.scan_id
end)
