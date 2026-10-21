local _, C = ...
function C:Import()
  local imported = CennikImport
  if type(imported) ~= "table" or imported.format_version ~= 1 then return end
  if imported.device_id and imported.device_id ~= self.db.device_id then
    -- First load can happen before Bridge writes its device id. Adopt it only
    -- while no task, report or uncertain purchase has ever been created.
    if next(self.db.tasks)==nil and #self.db.events==0 and #self.db.history==0 and not self.db.pending then
      self.db.device_id,self.db.sequence=imported.device_id,0
    else
      print("Cennik: другой Bridge device_id. Используйте прежний state-файл Bridge.")
      return
    end
  end
  local acknowledged = {}
  for _, id in ipairs(imported.acknowledged_event_ids or {}) do acknowledged[id] = true end
  local remaining = {}
  for _, event in ipairs(self.db.events) do if not acknowledged[event.event_id] then table.insert(remaining,event) end end
  self.db.events = remaining
  for _, package in ipairs(imported.packages or {}) do
    if package.format_version == 1 and type(package.tasks) == "table" and C:Integer(package.max_budget_copper) then
      self.db.current_market, self.db.current_run = package.market, package.run_id
      self.db.packages[package.package_id] = self.db.packages[package.package_id] or {budget=package.max_budget_copper,spent=0}
      for _, task in ipairs(package.tasks) do
        if task.format_version == 1 and task.market == package.market and task.run_id == package.run_id
          and type(task.task_id) == "string" and C:Integer(task.expires_at_epoch) and type(task.lines) == "table" then
          -- Imports never reset purchases, unknown operations or locally amended limits.
          if not self.db.tasks[task.task_id] then
            task.package_id, task.state, task.bought, task.spent = package.package_id,"pending_scan",{},0
            task.scans, task.selected_keys = {},{}
            self.db.tasks[task.task_id] = task
          else
            local old=self.db.tasks[task.task_id]
            if old.run_id==task.run_id and old.market==task.market and old.opportunity_id==task.opportunity_id then
              old.expires_at_epoch,old.expires_at=task.expires_at_epoch,task.expires_at
            end
          end
        end
      end
    end
  end
end
