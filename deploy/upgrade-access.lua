-- Only explicit, expiring deployment windows enter the waiting proxy.
local f = io.open('/run/wb2api-upgrade/active', 'r')
if f then
    local expires = tonumber(f:read('*l')) or 0
    f:close()
    if expires > ngx.now() then
        return ngx.exec('@wb2api_upgrade_wait')
    end
end
