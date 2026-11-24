local home = os.getenv("HOME")
local name_file_path = home .. "/.yradio/tmp/sel-file"
local conf_file_path = home .. "/.config/streamrip.conf"

local station_name = "Recordings"
local output_dir = ""
local track_counter = 1
local last_title = ""

-- Verklein de buffer zodat stream-record niet voorloopt op de metadata
-- mp.set_property("cache", "no")
-- mp.set_property("demuxer-readahead-secs", "0.5")
-- mp.set_property("audio-buffer", "0.1")

-- Verberg de afspeeltijd/buffer-teller in de terminal
mp.set_property("term-status-msg", "")

function on_metadata_change(name, title)
    if not title or title == "" then return end
    if title == last_title then return end
    last_title = title

    local final_filename = title

    if title == "stream" or title:find("remix") or title:find("radiobrowser") or title:find("%.m3u8") then
        final_filename = station_name
    end

    local safe_title = final_filename:gsub("[%c%?%*\\/<>|\"':]", "_"):gsub("[%.%s]+$", "")
    
    local ext = mp.get_property("file-format")
    local codec = mp.get_property("audio-codec-name")

    if ext == "hls" or ext == "lavf" or not ext or ext == "" then
        if codec == "aac" then
            ext = "aac"
        elseif codec == "mp3" then
            ext = "mp3"
        elseif codec == "vorbis" or codec == "opus" then
            ext = "ogg"
        else
            ext = "ts"
        end
    end

    local numbered_title = string.format("%03d - %s", track_counter, safe_title)
    local full_path = output_dir .. numbered_title .. "." .. ext

    print(" Recording [" .. string.format("%03d", track_counter) .. "] (" .. ext .. ") -> " .. full_path)

    -- Sluit het vorige bestand expliciet voordat het nieuwe start
    -- Probeer eerst de moderne property te zetten; als dat faalt, gebruik de oude
    if not mp.set_property("stream-record", full_path) then
        mp.set_property("record-file", full_path)
    end
    
    track_counter = track_counter + 1
end

local function start_ripping_after_delay()
    print(" Initialising... ")

    local file = io.open(name_file_path, "r")
    if file then
        local first_line = file:read("*l")
        file:close()
        if first_line and first_line ~= "" then
            station_name = (first_line:match("^[^|]*|([^|]*)") or first_line):gsub("^%s*(.-)%s*$", "%1"):gsub("[%c%?%*\\/<>|\"':]", "_")
        end
    end

    local base_dir = home
    local conf_file = io.open(conf_file_path, "r")
    if conf_file then
        local conf_line = conf_file:read("*l")
        conf_file:close()
        if conf_line and conf_line ~= "" then
            local cleaned_path = conf_line:gsub("^%s*(.-)%s*$", "%1"):gsub("/+$", "")
            if cleaned_path ~= "" then
                base_dir = cleaned_path
            end
        end
    end

    output_dir = base_dir .. "/" .. station_name .. "/"

    mp.command_native({
        name = "subprocess",
        args = { "mkdir", "-p", output_dir },
        playback_only = false
    })

    -- Gebruik filtered-metadata (reageert directer op ICY-tags dan media-title)
    mp.observe_property("filtered-metadata", "native", function(_, meta)
        if meta then
            local title = meta["icy-title"] or meta["title"] or meta["StreamTitle"]
            if title then
                on_metadata_change("metadata", title)
            end
        end
    end)

    -- Fallback voor streams die alleen media-title gebruiken (zoals sommige HLS streams)
    mp.observe_property("media-title", "string", on_metadata_change)
end

print(" Waiting... ")
mp.add_timeout(3.0, start_ripping_after_delay)
