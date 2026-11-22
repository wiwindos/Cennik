local _,C=...
local reasons={
  realm_unavailable="Игра скрыла имя сервера. Проверьте выбранный рынок и подтвердите его ниже для этого персонажа. До подтверждения сбор и закупки заблокированы.",
  catalog_missing="Каталог рынков ещё не получен. Нажмите «Обновить подключение» в Collector, дождитесь ответа сервера и выполните /reload.",
  realm_mismatch="Имя сервера игры отсутствует в каталоге. Передайте владельцу отчёт: он должен проверить имена сервера в вебе.",
  faction_mismatch="Для этой фракции нет подходящего рынка. Ключ и выбранный владельцем рынок должны соответствовать фракции персонажа.",
  client_mismatch="Этот рынок предназначен для клиента Forever 1.60.x. Проверьте папку игры в Collector.",
  ambiguous="Подходят несколько рынков. Выберите нужный в списке ниже.",
  bound="Сервер, фракция и рынок совпадают.",
}
function C:ForeverRealmIdentity(realm)
  if type(realm)~="string" then return end
  local ar,ak=realm:match("^forever%-(%a+)%-(%a+)$")
  if ar and ({eu=true,us=true,kr=true,tw=true,cn=true})[ar] and ({pvp=true,normal=true,rp=true,rppvp=true})[ak] then return ar,ak end
  local region,shard,number,kind=realm:upper():match("^CAMWOW ([A-Z]+)(%d+) REALM (%d+) ([%a%-]+)$")
  local rules={PVP="pvp",PVE="normal",RP="rp",["RP-PVP"]="rppvp"}
  if region and #shard<=3 and #number<=3 and ({EU=true,US=true,KR=true,TW=true,CN=true})[region] and rules[kind] then return region:lower(),rules[kind] end
end
function C:RealmAlias(binding)
  if not binding or binding.client_family~="forever" then return end
  local ruleset=binding.ruleset
  for _,name in ipairs(binding.realm_names or {}) do
    local region,kind=self:ForeverRealmIdentity(name)
    if region then
      if region~=binding.region or ruleset and ruleset~=kind then return end
      ruleset=kind
    end
  end
  if ruleset and ({eu=true,us=true,kr=true,tw=true,cn=true})[binding.region]
    and ({pvp=true,normal=true,rp=true,rppvp=true})[ruleset] then return "forever-" .. binding.region .. "-" .. ruleset end
end
function C:RealmMatchesBinding(binding,realm)
  if not binding then return false end
  local region,ruleset=self:ForeverRealmIdentity(realm)
  local expected=binding.ruleset
  if not expected then
    for _,name in ipairs(binding.realm_names or {}) do
      local r,k=self:ForeverRealmIdentity(name)
      if r then
        if r~=binding.region or expected and expected~=k then return false end
        expected=k
      end
    end
  end
  if binding.client_family=="forever" and region and expected then return region==binding.region and ruleset==expected end
  for _,name in ipairs(binding.realm_names or {}) do if name==realm then return true end end
  return false
end
function C:EquivalentRealms(left,right,binding)
  if type(left)=="string" and left~="" and left==right then return true end
  local lr,lk=self:ForeverRealmIdentity(left);local rr,rk=self:ForeverRealmIdentity(right)
  return binding and binding.client_family=="forever" and lr and lr==rr and lk==rk
    and self:RealmMatchesBinding(binding,left) and self:RealmMatchesBinding(binding,right) or false
end
function C:ResolveCollectorBinding(catalog,preferred)
  local faction=string.lower(UnitFactionGroup("player") or "")
  local version=GetBuildInfo and GetBuildInfo() or ""
  local candidates,realmFound,factionFound={},false,false
  for _,binding in ipairs(catalog or {}) do
      if self:RealmMatchesBinding(binding,C:RealmName()) then
        realmFound=true
        if binding.faction==faction then
          factionFound=true
          if binding.client_family~="forever" or version:match("^1%.60%.") then
            candidates[#candidates+1]=binding
          end
        end
      end
  end
  for _,binding in ipairs(candidates) do if preferred and binding.market==preferred then return binding,"bound",candidates end end
  if #candidates==1 then return candidates[1],"bound",candidates end
  local reason=(not catalog or #catalog==0) and "catalog_missing" or not realmFound and "realm_mismatch"
    or not factionFound and "faction_mismatch" or #candidates==0 and "client_mismatch" or "ambiguous"
  return nil,reason,candidates
end
function C:CollectorBindingInfo()
  local faction=string.lower(UnitFactionGroup("player") or "")
  local choices=self.db.collector_market_choices or {}
  local preferred=choices[self:CollectorChoiceKey()] or choices[C:RealmName() .. ":" .. faction]
  local binding,reason,candidates=self:ResolveCollectorBinding(CennikImport and CennikImport.market_catalog,preferred)
  local requires_confirmation=C:RealmName()==""
  if requires_confirmation then
    reason="realm_unavailable";candidates={}
    for _,entry in ipairs(CennikImport and CennikImport.market_catalog or {}) do
      if entry.faction==faction and self:RealmAlias(entry) then candidates[#candidates+1]=entry end
    end
  end
  if self:MarketBindingMatches(self.db.current_market) then reason="bound" end
  return {realm=C:RealmName(),faction=faction,reason=reason,message=reasons[reason] or reasons.catalog_missing,
    binding=binding,candidates=candidates,requires_confirmation=requires_confirmation,catalog=CennikImport and CennikImport.market_catalog or {}}
end
function C:CollectorChoiceKey()
  local region,ruleset=self:ForeverRealmIdentity(C:RealmName())
  local realm=region and ("forever:" .. region .. ":" .. ruleset) or C:RealmName()
  return realm .. ":" .. string.lower(UnitFactionGroup("player") or "")
end
function C:ChooseCollectorMarket(market)
  if not self.collectorOnly then return false,"Настройка относится к отдельному сборщику" end
  local info=self:CollectorBindingInfo()
  if info.requires_confirmation then
    local selected
    for _,entry in ipairs(info.candidates) do if entry.market==market then selected=entry end end
    local ok,reason=self:ConfirmRealmIdentity(selected)
    if not ok then return false,reason end
  end
  local binding,reason=self:ResolveCollectorBinding(info.catalog,market)
  if market and binding and binding.market~=market then return false,"Этот рынок не разрешён каталогом для текущего сервера и фракции" end
  if not binding then return false,reasons[reason] or "Рынок не разрешён для этого персонажа" end
  self.db.collector_market_choices=self.db.collector_market_choices or {}
  self.db.collector_market_choices[self:CollectorChoiceKey()]=binding.market
  return true,"Рынок выбран. Выполните /reload, чтобы применить подключение."
end
function C:CollectorBindingText()
  local info=self:CollectorBindingInfo()
  local out={"Сервер игры: " .. info.realm,"Фракция: " .. (({alliance="Альянс",horde="Орда"})[info.faction] or info.faction)}
  out[#out+1]="Рынок: " .. tostring(self.db.current_market or "не привязан")
  out[#out+1]=info.message
  for _,binding in ipairs(info.catalog) do
    out[#out+1]=(binding.label or binding.market) .. " · " .. tostring(binding.faction) .. "\nИзвестные имена сервера: " .. table.concat(binding.realm_names or {},", ")
  end
  return table.concat(out,"\n")
end
