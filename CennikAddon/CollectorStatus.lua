local _,C=...
local S={}
C.CollectorStatus=S
S.codes={
  AUCTION_ALREADY_REVIEWED={"ready","Тип аукциона уже подтверждён владельцем","Решение изменяется владельцем в веб-интерфейсе"},
  AUCTION_CONTEXT_UNKNOWN={"error","Тип аукциона не подтверждён; сбор и закупки заблокированы","Выберите предполагаемый тип в окне сборщика и передайте заявку владельцу"},
  AUCTION_PROPOSAL_PENDING={"wait","Тип предложен игроком: ожидается одобрение владельца","Сохраните через /reload или выход; выбор сам по себе не разрешает операции"},
  AUCTION_ARCHIVE_ONLY={"pause","Межфракционный аукцион: сохраняем только отдельный архив","Стратегии и закупки Cennik здесь отключены"},
  DISABLED={"pause","Сбор данных выключен","Включите автосбор в окне сборщика"},
  PASSIVE={"wait","Пассивный сбор включён; автоскан AH выключен","Сохраняем данные открытых NPC и профессий, фактический крафт и распыление"},
  WAIT_AUCTION={"wait","Ждём открытия аукциона","Откройте аукцион и оставайтесь в нём несколько секунд"},
  OPEN_DELAY={"wait","Аукцион открыт: ждём 3 секунды","Запрос начнётся, когда аукцион освободится"},
  UNBOUND={"error","Не подтверждён рынок персонажа","Подтвердите рынок в Cennik → Рынок"},
  BUSY={"wait","Аукцион занят поиском или покупкой","Сбор продолжится после завершения операции"},
  COOLDOWN={"pause","Ожидаем разрешённого интервала сканирования","Повторный полный запрос сейчас не нужен"},
  QUEUE_FULL={"error","Очередь снимков заполнена","Выполните /reload или выйдите из персонажа, дождитесь локального импорта и загрузите подтверждение через /reload"},
  UNSUPPORTED={"error","Совместимый API полного скана недоступен","Проверьте версию Auctionator и откройте диагностику"},
  SHARED_SCAN={"collect","Подхватываем общий полный скан","Дополнительный запрос не отправляется"},
  REQUESTED={"collect","Получаем полный снимок рынка","Оставьте аукцион открытым до окончания обработки"},
  PACKING={"collect","Обрабатываем строки снимка","Оставьте аукцион открытым"},
  READY={"ready","Снимок собран: ожидаем сохранения игрой","Данные попадут в приложение после /reload или штатного выхода"},
  SCAN_SAVED={"ready","Снимок передан приложению","Подтверждение загрузки на сервер показано отдельно ниже; следующий скан — после разрешённого интервала"},
  SCAN_INTERRUPTED={"error","Аукцион закрыт до завершения скана","Частичный результат не заменяет полный рынок; повторим после разрешённого интервала"},
  SCAN_TIMEOUT={"error","Сервер не прислал полный ответ за 120 секунд","Проверьте связь; повторим после разрешённого интервала"},
  SCAN_REJECTED={"error","Сервер отклонил запрос аукциона","Ждём разрешённого интервала, частые повторы не отправляем"},
  SCAN_FAILED={"error","Не удалось завершить полный скан","Проверьте Auctionator и передайте отчёт поддержки"},
  SCAN_PARTIAL={"error","Сохранён неполный снимок; рабочие цены не обновлены","Передайте сохранённое через /reload; повторим полный скан после разрешённого интервала"},
  SCAN_TOO_LARGE={"error","Снимок превышает поддерживаемый объём","Предыдущие полные снимки сохранены; передайте отчёт поддержки"},
  OBSERVATION_LIMIT={"error","Очередь наблюдений заполнена","Сохраните данные через /reload или выход и дождитесь импорта приложения"},
  RECIPE_API_UNAVAILABLE={"error","Не удалось прочитать рецепты профессии","Передайте отчёт поддержки: интерфейс профессий этой сборки не распознан"},
  SAVED={"ready","Последние данные импортированы приложением","Это подтверждение локального архива, доставка на сервер отображается отдельно"}
}
function S:Initialize()
  C.db.collector_status=C.db.collector_status or {events={},errors={},scans=0,rows=0}
  self.db=C.db.collector_status
  self.db.addon_version=C.VERSION
  self.db.build=GetBuildInfo and select(2,GetBuildInfo()) or nil
  if not self.db.collector_id then
    self.db.collector_id=C.db.client_id or C.db.device_id
  end
  local imported=CennikImport and CennikImport.collector_delivery
  if imported then
    self.db.delivery=C:Clone(imported)
    local ack={};for _,id in ipairs(imported.acknowledged_observation_ids or {}) do ack[id]=true end
    local kept={};for _,e in ipairs(C.db.collector_transforms or {}) do if not ack[e.observation_id] then kept[#kept+1]=e end end
    C.db.collector_transforms=kept
    local count=0;for key,e in pairs(C.db.collector_npc or {}) do if ack[e.observation_id] then C.db.collector_npc[key]=nil else count=count+1 end end
    C.db.collector_npc_count=count
  end
  if C.CollectorUI then C.CollectorUI:Initialize() end
end
function S:IsEnabled()
  return self.db and not self.db.paused and ((C.db.collector and C.db.collector.enabled) or (self.db.delivery and self.db.delivery.enabled))
end
function S:Set(code,detail)
  if not self.db then return end
  local spec=self.codes[code] or self.codes.SCAN_FAILED
  local previous=self.db.current
  if previous and previous.code==code then
    previous.detail=detail or previous.detail
    return
  end
  local value={code=code,level=spec[1],message=spec[2],action=spec[3],at=math.floor(C:Now()),detail=detail or ""}
  self.db.current=value;self.db.events[#self.db.events+1]=C:Clone(value)
  while #self.db.events>20 do table.remove(self.db.events,1) end
  if value.level=="error" then
    local error=self.db.errors[code] or {count=0};self.db.errors[code]=error
    error.count=error.count+1;error.at=value.at;self.db.last_error=C:Clone(value)
    print("Cennik · сбор: " .. value.message .. " [" .. code .. "]. " .. value.action)
  end
  if C.CollectorUI then C.CollectorUI:Refresh() end
end
function S:Finished(scan)
  if not self.db then return end
  self.db.scans=self.db.scans+1;self.db.rows=self.db.rows+(scan.count or 0)
  if scan.full then self.db.last_success={at=scan.observed_at,rows=scan.count};self:Set("READY")
  elseif self.db.current and self.db.current.code=="SCAN_INTERRUPTED" then return
  elseif (scan.error or ""):find("120",1,true) then self:Set("SCAN_TIMEOUT")
  else self:Set("SCAN_PARTIAL",tostring(scan.count or 0) .. " / " .. tostring(scan.reported_count or 0) .. " строк. " .. (scan.error or "Неполное покрытие")) end
end
function S:Summary()
  local d=self.db or {};local current=d.current or {};local success=d.last_success
  local delivery=d.delivery or {}
  local latest=C.db.collector.last_scan
  local out={"Cennik · сбор данных " .. (self:IsEnabled() and "включён" or "выключен"),
    C.AuctionContext and C.AuctionContext:Summary() or "",
    current.message or "Ожидание",current.detail or "",current.action or "",
    "Последний полный снимок: " .. (success and date("%d.%m %H:%M:%S",success.at) .. " · " .. success.rows .. " строк" or "ещё нет"),
    "Последняя попытка: " .. (latest and ((latest.full and "полная" or "НЕПОЛНАЯ") .. " · " .. tostring(latest.count or 0) .. " / " .. tostring(latest.reported_count or 0) .. " строк") or "ещё нет"),
    "В памяти игры ожидают записи/локального подтверждения: " .. #(C.db.market_scans or {}) .. " снимков",
    "Ротация: последние 3 снимка, до 48 МБ на снимок / 128 МБ в очереди. Сейчас: " .. string.format("%.1f",C.MarketCollector:QueuedBytes()/1000000) .. " МБ. Заменено до выгрузки: " .. (C.db.collector.rotated or 0),
    "Собрано этим аддоном: " .. (d.scans or 0) .. " снимков / " .. (d.rows or 0) .. " строк",
    "Локальный импорт: " .. (delivery.local_at and date("%d.%m %H:%M:%S",delivery.local_at) or "подтверждения ещё нет"),
    "Сервер: " .. (delivery.message or "подключение не настроено") .. (delivery.at and " · подтверждение от " .. date("%d.%m %H:%M:%S",delivery.at) or ""),
    "Статус сервера загружается через /reload и не является прямой связью из игры."}
  if d.last_error then out[#out+1]="Последняя ошибка: " .. d.last_error.code .. " · " .. d.last_error.message end
  if C.Automation then out[#out+1]=C.Automation:Summary() end
  return table.concat(out,"\n")
end
function S:Report()
  local version,build=GetBuildInfo()
  local out={"Cennik Collector support v1","Collector ID: " .. tostring(self.db.collector_id),
    "Cennik: " .. tostring(C.VERSION),"Game: " .. tostring(version) .. " / " .. tostring(build),
    "Auctionator: " .. tostring(C_AddOns and C_AddOns.GetAddOnMetadata and C_AddOns.GetAddOnMetadata("Auctionator","Version") or GetAddOnMetadata and GetAddOnMetadata("Auctionator","Version") or "unknown"),
    "Market: " .. tostring(C.db.collector and C.db.collector.market),self:Summary(),"Recent events:"}
  if C.Adapter.error then out[#out+1]="Adapter: " .. C.Adapter.error end
  for _,e in ipairs(self.db.events) do out[#out+1]=date("%d.%m %H:%M:%S",e.at) .. " · " .. e.code end
  out[#out+1]="Error counts:"
  for code,e in pairs(self.db.errors) do out[#out+1]=code .. " × " .. e.count end
  return table.concat(out,"\n")
end
function S:Observe(event)
  if not self:IsEnabled() or event.confirmation~="client" or (event.kind~="disenchant" and event.kind~="craft") then return end
  if not C:MarketBindingMatches(C.db.collector and C.db.collector.market or C.db.current_market) then return end
  C.db.collector_transforms=C.db.collector_transforms or {}
  if #C.db.collector_transforms>=1000 then self:Set("OBSERVATION_LIMIT");return end
  C.db.collector_observation_sequence=(C.db.collector_observation_sequence or 0)+1
  local function stock(lines)
    local result={};for _,line in ipairs(lines or {}) do result[#result+1]={item_id=line.item_id,quantity=line.quantity,variant=line.variant} end;return result
  end
  C.db.collector_transforms[#C.db.collector_transforms+1]={kind=event.kind,observed_at=event.observed_at,
    observation_id=C.db.device_id .. ":observation:" .. C.db.collector_observation_sequence,
    spell_id=event.details and event.details.recipe_id,inputs=stock(event.inputs),outputs=stock(event.outputs)}
end
function S:CaptureRecipes()
  if not self:IsEnabled() then return end
  if not C:MarketBindingMatches(C.db.collector and C.db.collector.market or C.db.current_market) then return end
  local r=C:Clone(C.db.collector_recipes or {recipes={},coverage="visible_recipes_only"})
  r.observed_at=math.floor(C:Now())
  if GetNumTradeSkills and GetTradeSkillRecipeLink and GetTradeSkillItemLink and GetTradeSkillNumReagents
    and GetTradeSkillReagentInfo and GetTradeSkillReagentItemLink then
    for i=1,math.min(5000,GetNumTradeSkills() or 0) do
      local link=GetTradeSkillRecipeLink(i)
      local spell=link and tonumber(link:match("enchant:(%d+)") or link:match("spell:(%d+)"))
      if spell then
        local output=GetTradeSkillItemLink and C.Accounting:Line(GetTradeSkillItemLink(i),1)
        local inputs={}
        for j=1,(GetTradeSkillNumReagents and GetTradeSkillNumReagents(i) or 0) do
          local _,_,q=GetTradeSkillReagentInfo(i,j)
          local line=C.Accounting:Line(GetTradeSkillReagentItemLink(i,j),q)
          if line then inputs[#inputs+1]={item_id=line.item_id,quantity=line.quantity} end
        end
        local qmin,qmax
        if GetTradeSkillNumMade then qmin,qmax=GetTradeSkillNumMade(i) end
        r.recipes[tostring(spell)]={spell_id=spell,item_id=output and output.item_id,name=output and output.item_name,inputs=inputs,qty_min=qmin,qty_max=qmax,observed_at=r.observed_at}
      end
    end
  elseif C_TradeSkillUI and C_TradeSkillUI.GetAllRecipeIDs and C_TradeSkillUI.GetRecipeInfo then
    for _,id in ipairs(C_TradeSkillUI.GetAllRecipeIDs() or {}) do
      local info=C_TradeSkillUI.GetRecipeInfo(id)
      if info and info.learned then
        -- Alternatives and optional reagents require a schematic; recipe access
        -- alone must never fabricate a complete material list.
        local observed=C:ObservedModernRecipe(id,info.name)
        observed.observed_at=r.observed_at
        r.recipes[tostring(id)]=observed
      end
    end
  else self:Set("RECIPE_API_UNAVAILABLE");return end
  C.db.collector_recipes=r
end
local events=CreateFrame("Frame")
for _,event in ipairs({"TRADE_SKILL_SHOW","TRADE_SKILL_LIST_UPDATE"}) do pcall(events.RegisterEvent,events,event) end
events:SetScript("OnEvent",function()
  if S.recipeScheduled then return end;S.recipeScheduled=true
  C_Timer.After(.25,function() S.recipeScheduled=false;S:CaptureRecipes() end)
end)
