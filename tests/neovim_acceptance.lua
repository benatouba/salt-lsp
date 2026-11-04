-- Standard Neovim LSP only: no editor plugin or Salt runtime.
local root = assert(os.getenv('SALT_LSP_ACCEPTANCE_ROOT'))
local settings = vim.json.decode(assert(os.getenv('SALT_LSP_ACCEPTANCE_SETTINGS')))
vim.cmd.edit(root .. '/state.sls')
vim.bo.filetype = 'sls'
local state = vim.api.nvim_get_current_buf()
local id = assert(vim.lsp.start({
  name = 'salt-acceptance',
  cmd = { assert(os.getenv('SALT_LSP_ACCEPTANCE_COMMAND')) },
  root_dir = root,
  init_options = { salt = settings },
  flags = { debounce_text_changes = 0 },
}))
local client = assert(vim.lsp.get_client_by_id(id))
assert(vim.wait(10000, function() return client.initialized end, 20), 'initialize timed out')

local function request(method, params, buffer)
  local response = assert(client:request_sync(method, params, 10000, buffer or state), method .. ' timed out')
  assert(not response.err, vim.inspect(response.err))
  return response.result
end
local function text(source, buffer)
  vim.api.nvim_buf_set_lines(buffer or state, 0, -1, false, vim.split(source, '\n', { trimempty = true }))
end
local function at(method, line, character, buffer)
  buffer = buffer or state
  return request('textDocument/' .. method, { textDocument = { uri = vim.uri_from_bufnr(buffer) }, position = { line = line, character = character } }, buffer)
end
local function has(items, label)
  for _, item in ipairs(items.items or items) do
    if item.label == label then return item end
  end
  error('Missing completion ' .. label .. ': ' .. vim.inspect(items))
end
local function symbols()
  return request('textDocument/documentSymbol', { textDocument = { uri = vim.uri_from_bufnr(state) } })
end
local function tool(name)
  return { uri = vim.uri_from_bufnr(state), tool = name }
end
local function approve(name)
  local params = tool(name)
  local status = request('salt/toolStatus', params)
  assert(status.status == 'denied-trust', vim.inspect(status))
  params.action, params.token = 'allow', status.token
  assert(request('salt/authorizeTool', params).status == 'ready')
end

assert(client.server_capabilities.documentFormattingProvider)
has(at('completion', 2, 9), 'source')
assert(at('hover', 1, 5).contents.value:find('3008.5', 1, true))
assert(#symbols() > 0)
settings.projects[1].activeProfile = 'older'
client:notify('workspace/didChangeConfiguration', { settings = { salt = settings } })
assert(at('hover', 1, 5).contents.value:find('3006.30', 1, true))
settings.projects[1].activeProfile = 'default'
client:notify('workspace/didChangeConfiguration', { settings = { salt = settings } })
text("{{ salt['acme.lookup']('x') }}\nexample:\n  acme.managed:\n    - name: x")
assert(at('hover', 0, 15).contents.value:find('Local execution documentation', 1, true))
assert(at('hover', 2, 6).contents.value:find('Project declaration', 1, true))
local locations = at('definition', 2, 6)
assert(locations[1].uri == vim.uri_from_fname(root .. '/_states/acme.py'))

-- Attach to Python only to keep its unsaved source synchronized. The Salt
-- server must not claim generic Python completion or formatting ownership.
local python = vim.fn.bufadd(root .. '/_states/acme.py')
vim.fn.bufload(python)
vim.bo[python].filetype = 'python'
assert(vim.lsp.buf_attach_client(python, id))
text("raise RuntimeError('DO NOT EXECUTE')\ndef fresh(name, current=None):\n    '''Unsaved project documentation.'''\n    pass", python)
request('textDocument/documentSymbol', { textDocument = { uri = vim.uri_from_bufnr(python) } }, python)
text('example:\n  acme.fresh:\n    - cur')
has(at('completion', 2, 9), 'current')
locations = at('definition', 1, 6)
assert(locations[1].uri == vim.uri_from_bufnr(python) and locations[1].range.start.line == 1)
local generic = at('completion', 1, 5, python)
assert(not generic or #generic.items == 0)
assert(#request('textDocument/formatting', { textDocument = { uri = vim.uri_from_bufnr(python) }, options = { tabSize = 2, insertSpaces = true } }, python) == 0)

text('include:\n  - kn')
has(at('completion', 1, 6), 'known')
text('include:\n  - known\nconsumer:\n  test.nop:\n    - require:\n      - test: target')
locations = at('definition', 1, 6)
assert(locations[1].uri == vim.uri_from_fname(root .. '/known.sls'))
locations = at('definition', 5, 16)
assert((locations.uri or locations[1].uri) == vim.uri_from_fname(root .. '/known.sls'))
text('same:\n  test.nop: []\nsame:\n  test.nop: []')
symbols()
assert(vim.wait(3000, function()
  for _, diagnostic in ipairs(vim.diagnostic.get(state)) do
    if diagnostic.source == 'salt-lsp' and diagnostic.code == 'duplicate-id' then return true end
  end
  return false
end, 20), 'built-in diagnostic missing')

text('value: trailing  ')
if os.getenv('SALT_LSP_TEST_SALT_LINT') then
  approve('salt-lint')
  local lint_status
  assert(vim.wait(10000, function()
    lint_status = request('salt/lintStatus', { uri = vim.uri_from_bufnr(state) })
    return lint_status.status == 'findings'
  end, 100), 'trusted lint did not complete: ' .. vim.inspect(lint_status))
  local found = false
  for _, diagnostic in ipairs(vim.diagnostic.get(state)) do
    if diagnostic.source == 'salt-lint' then found = true end
  end
  assert(found, 'native lint finding missing')
  text('value: clean')
  symbols()
  for _, diagnostic in ipairs(vim.diagnostic.get(state)) do assert(diagnostic.source ~= 'salt-lint') end
else
  assert(request('salt/toolStatus', tool('salt-lint')).status == 'missing-tool')
end
local function format()
  return request('textDocument/formatting', { textDocument = { uri = vim.uri_from_bufnr(state) }, options = { tabSize = 2, insertSpaces = true } })
end
text('key:\n    nested: value')
if os.getenv('SALT_LSP_TEST_YAMLFMT') then
  approve('yamlfmt')
  local edits = format()
  assert(#edits == 1 and edits[1].newText == 'key:\n  nested: value\n')
  assert(vim.api.nvim_buf_get_lines(state, 1, 2, false)[1] == '    nested: value', 'server rewrote buffer')
else
  assert(request('salt/toolStatus', tool('yamlfmt')).status == 'missing-tool')
  assert(#format() == 0)
end
text('key: {{ grains.id }}')
assert(#format() == 0, 'Jinja formatting must be refused')
client:stop()
assert(vim.wait(10000, function() return client:is_stopped() end, 20), 'server shutdown timed out')
print('Neovim standard LSP checks passed')
vim.cmd('qa!')
