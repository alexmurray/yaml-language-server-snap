-- Drive yaml-language-server with Neovim's built-in LSP client, as an editor
-- would: open a valid and an invalid document from the workspace given as
-- the first argument, then check diagnostics, hover, completion and
-- formatting against the workspace's JSON schema.
-- nvim is a classic snap, so runs under a complain-mode AppArmor profile,
-- and snap-confine's profile denies inheriting file descriptors created
-- under that label - so `snap run yaml-language-server` silently exits when
-- given the unix socketpair nvim creates for its stdio (LP: #1849753, seen
-- on 20.04). Like the opencode snap's CLI wrapper, arm an AppArmor onexec
-- transition and re-run this script in a fresh nvim, which then runs (and
-- creates the server's socketpair) unconfined - nvim has no exec(), so the
-- original process just relays the child's output and exit code.
local attr_dir = vim.uv.fs_stat('/proc/thread-self/attr/apparmor/current')
  and '/proc/thread-self/attr/apparmor/' or '/proc/thread-self/attr/'
local f = io.open(attr_dir .. 'current')
local label = f and f:read('*a') or ''
if f then f:close() end
if label:match('^snap%.nvim%.') and label:match('%(complain%)%s*$') then
  f = assert(io.open(attr_dir .. 'exec', 'w'))
  assert(f:write('exec unconfined'))
  assert(f:close())
  local relay = function(out)
    return function(_, data)
      if data then out:write(data) end
    end
  end
  local res = vim.system({ vim.v.progpath, '--clean', '-l', arg[0], unpack(arg) }, {
    stdout = relay(io.stdout),
    stderr = relay(io.stderr),
  }):wait()
  os.exit(res.code)
end

local root = vim.fn.fnamemodify(arg[1], ':p'):gsub('/$', '')
local timeout = 120000

local function fail(msg)
  -- the server's stderr ends up in the LSP log
  local log = io.open(vim.lsp.log.get_filename())
  if log then
    io.stderr:write(log:read('*a'))
    log:close()
  end
  io.stderr:write(msg .. '\n')
  os.exit(1)
end

local function open(name)
  vim.cmd.edit(root .. '/' .. name)
  local buf = vim.api.nvim_get_current_buf()
  local id = vim.lsp.start({
    name = 'yamlls',
    cmd = { 'yaml-language-server', '--stdio' },
    root_dir = root,
    settings = {
      yaml = {
        validate = true,
        hover = true,
        completion = true,
        format = { enable = true },
        -- keep the tests independent of the internet
        schemaStore = { enable = false },
      },
    },
  })
  return buf, vim.lsp.get_client_by_id(id)
end

local function request(client, buf, method, params)
  local res, err = client:request_sync(method, params, timeout, buf)
  if not res or res.err then
    fail(method .. ' failed: ' .. vim.inspect(err or res))
  end
  return res.result
end

local function wait_diagnostic(buf, msg)
  local found = vim.wait(timeout, function()
    for _, d in ipairs(vim.diagnostic.get(buf)) do
      if d.message:find(msg, 1, true) then
        print('diagnostic: ' .. d.lnum + 1 .. ':' .. d.col + 1 .. ': ' .. d.message)
        return true
      end
    end
    return false
  end)
  if not found then
    fail('timed out waiting for diagnostic "' .. msg .. '": '
      .. vim.inspect(vim.diagnostic.get(buf)))
  end
end

local pet, client = open('pet.yaml')
if not vim.wait(timeout, function() return client.initialized end) then
  fail('timed out waiting for yaml-language-server to initialize')
end

-- LSP positions are 0-based: line 2 is `kind: dog`
local hover = request(client, pet, 'textDocument/hover', {
  textDocument = { uri = vim.uri_from_bufnr(pet) },
  position = { line = 2, character = 1 },
}).contents.value
print('hover: ' .. hover)
if not hover:find('What sort of animal the pet is', 1, true) then
  fail('unexpected hover result')
end

-- the schema is only applied once loaded, so check for the violations it
-- reports before relying on it for completion
local broken = open('broken.yaml')
wait_diagnostic(broken, 'Value is not accepted. Valid values: "cat", "dog", "fish".')
wait_diagnostic(broken, 'Incorrect type. Expected "integer".')
if #vim.diagnostic.get(pet) ~= 0 then
  fail('unexpected diagnostics for pet.yaml: ' .. vim.inspect(vim.diagnostic.get(pet)))
end

-- edit the valid document to a key with no value yet, which should then
-- complete to the schema's enum values
vim.api.nvim_set_current_buf(pet)
vim.api.nvim_buf_set_lines(pet, 2, 3, true, { 'kind: ' })
local result = request(client, pet, 'textDocument/completion', {
  textDocument = { uri = vim.uri_from_bufnr(pet) },
  position = { line = 2, character = 6 },
})
local labels = {}
for _, item in ipairs(result.items or result) do
  table.insert(labels, item.label)
end
table.sort(labels)
print('completions: ' .. table.concat(labels, ' '))
if table.concat(labels, ' ') ~= 'cat dog fish' then
  fail('unexpected completions')
end

-- and the now missing required value is reported
wait_diagnostic(pet, 'Incorrect type. Expected "string".')

-- formatting through the editor tidies up the buffer
vim.api.nvim_buf_set_lines(pet, 1, -1, true, { 'name:    Rex', 'kind:   dog', 'age:   3' })
vim.lsp.buf.format({ bufnr = pet, timeout_ms = timeout })
local lines = table.concat(vim.api.nvim_buf_get_lines(pet, 1, -1, true), '\n')
print('formatted: ' .. lines)
if lines ~= 'name: Rex\nkind: dog\nage: 3' then
  fail('unexpected formatting')
end

client:stop()
if not vim.wait(timeout, function() return client:is_stopped() end) then
  fail('yaml-language-server did not shut down')
end
os.exit(0)
