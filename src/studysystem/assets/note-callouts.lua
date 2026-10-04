-- The callout boxes (D-80, D-81): pandoc runs this on a note while turning it into the page
-- Chrome prints. A paragraph or bullet that starts with a note marker becomes a box - a div with
-- classes `callout` and its kind, styled by note.css; a run of the same marker joins into one box
-- with one title.
-- A line naming more than one marker (the legend) stays plain; a marker mid-sentence stays inline.

local MARKERS = {
  { prefix = "\u{26A0}", kind = "warning", title = "\u{26A0}\u{FE0F} Common mistake" },
  { prefix = "\u{1F4A1}", kind = "tip", title = "\u{1F4A1} Tip" },
  { prefix = "\u{1F3AF}", kind = "target", title = "\u{1F3AF} Exam target" },
}

local function marker_of(block)
  if block.t ~= "Para" and block.t ~= "Plain" then return nil end
  local text = pandoc.utils.stringify(block)
  -- the legend line names all three markers: it is not a callout
  local found = 0
  for _, m in ipairs(MARKERS) do
    if text:find(m.prefix, 1, true) then found = found + 1 end
  end
  if found > 1 then return nil end
  for _, m in ipairs(MARKERS) do
    if text:sub(1, #m.prefix) == m.prefix then return m end
  end
  return nil
end

-- drop the leading marker (and its variation selector and space) from a block's inlines
local function strip_marker(block)
  local inl = block.content
  while #inl > 0 do
    local first = inl[1]
    if first.t == "Space" then
      table.remove(inl, 1)
    elseif first.t == "Str" then
      local s = first.text
      -- whole UTF-8 sequences: a Lua character class would cut an emoji byte by byte
      local rest = s
      for _, m in ipairs(MARKERS) do
        if rest:sub(1, #m.prefix) == m.prefix then rest = rest:sub(#m.prefix + 1) end
      end
      if rest:sub(1, 3) == "\u{FE0F}" then rest = rest:sub(4) end
      if rest == s then break end
      if rest == "" then table.remove(inl, 1) else first.text = rest; break end
    else
      break
    end
  end
  return pandoc.Para(inl)
end

local function box(m, blocks)
  local content = { pandoc.Para({ pandoc.Strong({ pandoc.Str(m.title) }) }) }
  for _, b in ipairs(blocks) do table.insert(content, b) end
  return pandoc.Div(content, pandoc.Attr("", { "callout", m.kind }))
end

-- walk a block list: group consecutive marker paragraphs, and lift marker items out of lists
local function rewrite(blocks)
  local out, run, run_m = {}, {}, nil
  local function flush()
    if run_m then table.insert(out, box(run_m, run)) end
    run, run_m = {}, nil
  end
  local function add_marked(m, item_blocks)
    if run_m ~= m then flush(); run_m = m end
    item_blocks[1] = strip_marker(item_blocks[1])
    for _, b in ipairs(item_blocks) do table.insert(run, b) end
  end
  for _, b in ipairs(blocks) do
    local m = marker_of(b)
    if m then
      add_marked(m, { b })
    elseif b.t == "BulletList" then
      local plain = {}
      for _, item in ipairs(b.content) do
        local im = item[1] and marker_of(item[1])
        if im then
          if #plain > 0 then flush(); table.insert(out, pandoc.BulletList(plain)); plain = {} end
          add_marked(im, { table.unpack(item) })
        else
          flush()
          table.insert(plain, item)
        end
      end
      flush()
      if #plain > 0 then table.insert(out, pandoc.BulletList(plain)) end
    else
      flush()
      table.insert(out, b)
    end
  end
  flush()
  return out
end

function Pandoc(doc)
  doc.blocks = rewrite(doc.blocks)
  return doc
end
