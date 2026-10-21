local _, C = ...
local Q = {}
C.BuyQueue = Q
function Q:Add(task)
  if task.hidden then return false,"Задание скрыто игроком" end
  if task.state~="ready_to_buy" then return false,"Сначала проверьте задание" end
  for _, id in ipairs(C.db.queue) do if id==task.task_id then return false,"Уже в очереди" end end
  table.insert(C.db.queue,task.task_id)
  C.db.queue_stopped=false
  C:Refresh()
  return true
end
function Q:Task()
  for _, id in ipairs(C.db.queue) do
    local task=C.db.tasks[id]
    if task and not task.hidden and task.state~="completed" and task.state~="cancelled" then return task end
  end
end
function Q:Prepare()
  if C.db.queue_stopped or C.db.pending then return false end
  local task=self:Task()
  if not task then return false end
  self.ticket=nil
  return C.Scanner:Start(task,function(_,validation)
    if validation.state=="ready_to_buy" then
      local plan=validation.plans[1]
      local lot=plan.commodity and {item_id=plan.item_id,quantity=plan.quantity,cost=plan.cost,commodity=true,key=plan.key,
        limit=plan.limit,observed_at=C:Now()} or plan.lots[1]
      Q.ticket={task_id=task.task_id,lot=lot,validation=validation,prepared_at=C:Now()}
      C:Refresh()
    end
  end)
end
function Q:Skip()
  if C.db.pending then return false end
  C.Scanner:Cancel("Позиция пропущена")
  local task=self:Task()
  if task then C:State(task,"cancelled","Позиция пропущена игроком; частичная закупка сохранена") end
  self.ticket=nil
  return true
end
function Q:Stop()
  C.db.queue_stopped=true
  self.ticket=nil
  C.Scanner:Cancel("Очередь остановлена")
  C.Execution:Stop()
  C:Refresh()
end
