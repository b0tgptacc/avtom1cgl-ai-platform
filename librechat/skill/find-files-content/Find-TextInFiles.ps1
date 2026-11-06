<#
.SYNOPSIS
    Поиск файлов, в тексте которых встречаются слова или фразы: Word (docx, doc), Excel (xlsx, xls), PowerPoint (pptx,
    ppt), OpenDocument (odt, ods), RTF, текстовые файлы (txt, csv, log, xml, json, md, ini, sql) в UTF-8 и Windows-1251.
    Запускается вручную на компьютере сотрудника (Windows PowerShell 5.1 или PowerShell 7). Ничего не устанавливает,
    файлы только читает (в том числе открытые в Word/Excel), ничего не отправляет в сеть.
    Список найденного — CSV для Excel (по умолчанию на рабочий стол). Журнал: %LOCALAPPDATA%\Find-TextInFiles.log

.EXAMPLE
    .\Find-TextInFiles.ps1 -Path "D:\Документы" -Pattern "Ромашка"
.EXAMPLE
    .\Find-TextInFiles.ps1 -Path "D:\Документы,\\server\share\Договоры" -Pattern "Ромашка,7707083893" -All
    Несколько папок и слов — через запятую. С -Regex запятая в -Pattern не разделяет (она бывает частью выражения).
.EXAMPLE
    .\Find-TextInFiles.ps1 -Pattern "договор* поставки" -Include docx,doc
    Без -Path — вся папка пользователя (C:\Users\<имя>), кроме AppData. «*» в конце слова — любое окончание
    (договор* → договора, договору); «е» находит и «ё».
.EXAMPLE
    .\Find-TextInFiles.ps1 -Path "C:\Work" -Pattern '\b\d{10}\b' -Regex -Include docx,xlsx

.NOTES
    Если запуск скриптов запрещён: powershell -ExecutionPolicy Bypass -File .\Find-TextInFiles.ps1 ...
    PDF не читается (текст в PDF хранится не по буквам) — скрипт сообщает, сколько PDF в папке, и подсказывает,
    как найти в них: поиск Windows (содержимое:слово) или прикрепить эти PDF в чат помощнику.
#>
[CmdletBinding()]
param(
    [string[]] $Path = @($env:USERPROFILE),
    [Parameter(Mandatory)] [string[]] $Pattern,
    [string[]] $Include = @('txt','csv','log','xml','json','md','ini','sql','rtf',
                            'docx','xlsx','pptx','odt','ods','doc','xls','ppt'),
    [switch]   $All,             # файл подходит, только если найдены ВСЕ слова (по умолчанию — любое)
    [switch]   $Regex,           # слова — регулярные выражения
    [switch]   $CaseSensitive,
    [int]      $MaxSizeMB = 50,
    [string]   $OutCsv,          # по умолчанию — «Поиск_<дата>.csv» на рабочем столе
    [switch]   $NoCsv,           # только показать на экране
    [string[]] $SkipFolders = @('AppData','$Recycle.Bin','Windows','Program Files','Program Files (x86)','ProgramData',
                                'node_modules','.git')
)

# при запуске через -File список "а","б" приходит одной строкой "а,б" — разбираем запятые сами
function Split-List([string[]]$items) { @($items | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
if (-not $Regex) { $Pattern = Split-List $Pattern }
$Path = Split-List $Path
$Include = Split-List $Include
$SkipFolders = Split-List $SkipFolders

$log = Join-Path $env:LOCALAPPDATA 'Find-TextInFiles.log'
if (-not $env:LOCALAPPDATA) { $log = Join-Path ([System.IO.Path]::GetTempPath()) 'Find-TextInFiles.log' }
Set-Content -Path $log -Value $null -Encoding UTF8
function Log([string]$msg, [switch]$Quiet) {
    $line = '{0:HH:mm:ss} {1}' -f (Get-Date), $msg
    if (-not $Quiet) { Write-Host $msg }
    Add-Content -Path $log -Value $line -Encoding UTF8
}
Log ("{0} на {1}, PowerShell {2}" -f $env:USERNAME, $env:COMPUTERNAME, $PSVersionTable.PSVersion) -Quiet
Log ("Где: {0}; что: {1}; типы: {2}" -f ($Path -join ', '), ($Pattern -join ' | '), ($Include -join ',')) -Quiet

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
try { [System.Text.Encoding]::RegisterProvider([System.Text.CodePagesEncodingProvider]::Instance) } catch { }
$cp1251 = [System.Text.Encoding]::GetEncoding(1251)
$utf8Strict = New-Object System.Text.UTF8Encoding($false, $true)

$opts = [System.Text.RegularExpressions.RegexOptions]::None
if (-not $CaseSensitive) { $opts = [System.Text.RegularExpressions.RegexOptions]::IgnoreCase }
$regexes = @(foreach ($p in $Pattern) {
    if ($Regex) { New-Object regex($p, $opts); continue }
    # «е» и «ё» — одна буква; «*» — любое окончание слова; пробелы — любые пробелы и переносы строк
    $rx = [regex]::Escape($p) -creplace '[её]', '[её]' -creplace '[ЕЁ]', '[ЕЁ]' -replace '\\\*', '\w*' -replace '(\\ )+', '\s+'
    New-Object regex($rx, $opts)
})

# какие части архива Office/OpenDocument содержат текст
$zipParts = @{
    docx = '^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$'
    xlsx = '^xl/(sharedStrings|worksheets/sheet\d+)\.xml$'
    pptx = '^ppt/(slides/slide\d+|notesSlides/notesSlide\d+)\.xml$'
    odt  = '^content\.xml$'
    ods  = '^content\.xml$'
}
$binary = @('doc', 'xls', 'ppt')

# открыть только на чтение, не мешая Word/Excel, у которых файл открыт
function Open-Shared([string]$file) {
    New-Object System.IO.FileStream($file, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
                                    [System.IO.FileShare]::ReadWrite)
}

function Read-Bytes([string]$file) {
    $fs = Open-Shared $file
    try {
        $ms = New-Object System.IO.MemoryStream
        $fs.CopyTo($ms)
        return $ms.ToArray()
    } finally { $fs.Dispose() }
}

function Read-PlainText([string]$file) {
    $bytes = Read-Bytes $file
    try { return $utf8Strict.GetString($bytes).TrimStart([char]0xFEFF) } catch { return $cp1251.GetString($bytes) }
}

function Read-RtfText([string]$file) {
    # кириллица в RTF — \'e0 (байт Windows-1251) или \u1072? (номер символа)
    $rtf = $cp1251.GetString((Read-Bytes $file))
    $rtf = [regex]::Replace($rtf, "\\'([0-9a-fA-F]{2})", { param($m) $cp1251.GetString([byte[]]@([Convert]::ToByte($m.Groups[1].Value, 16))) })
    $rtf = [regex]::Replace($rtf, '\\u(-?\d+)\??', { param($m) [string][char](([int]$m.Groups[1].Value + 65536) % 65536) })
    $rtf = $rtf -replace '\\par[d]?\b', "`n" -replace '\\[a-zA-Z]+-?\d* ?', '' -replace '[{}]', ''
    return $rtf
}

function Read-BinaryText([string]$file) {
    # старые doc/xls/ppt: кириллица хранится в UTF-16 (строка может начинаться и с нечётного байта), латиница
    # и цифры — часто по байту. Читаем всеми способами (как «С текстом» в Total Commander); служебные байты — пробелы
    $bytes = Read-Bytes $file
    $shifted = New-Object byte[] ([Math]::Max(0, $bytes.Length - 1))
    [Array]::Copy($bytes, 1, $shifted, 0, $shifted.Length)
    $text = [System.Text.Encoding]::Unicode.GetString($bytes) + "`n" + [System.Text.Encoding]::Unicode.GetString($shifted) +
            "`n" + $cp1251.GetString($bytes)
    # всё, кроме русских и латинских букв, цифр и обычных знаков, — граница куска текста (перевод строки)
    return $text -replace '[^\u0020-\u007E\u00AB\u00BB\u0401\u0410-\u044F\u0451\u2013\u2014\u2026\u2116\t]+', "`n"
}

function Read-ZipText([string]$file, [string]$partsPattern) {
    $fs = Open-Shared $file
    $zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Read)
    try {
        $sb = New-Object System.Text.StringBuilder
        foreach ($entry in $zip.Entries) {
            if ($entry.FullName -notmatch $partsPattern) { continue }
            $reader = New-Object System.IO.StreamReader($entry.Open(), [System.Text.Encoding]::UTF8)
            try { $xml = $reader.ReadToEnd() } finally { $reader.Dispose() }
            # конец абзаца/строки/ячейки — перевод строки; остальные теги убрать без пробела
            # (в Word слово бывает разбито на несколько фрагментов <w:t>)
            $xml = $xml -replace '</w:p>|</a:p>|</si>|</row>|</c>|</text:p>|</table:table-cell>|<w:tab/>|<w:br/>', "`n"
            $text = $xml -replace '<[^>]+>', ''
            [void]$sb.AppendLine([System.Net.WebUtility]::HtmlDecode($text))
        }
        return $sb.ToString()
    } finally { $zip.Dispose(); $fs.Dispose() }
}

# обход папок без системных (AppData и т. п.) и без ссылок-переходов (junction), которые ведут по кругу
$stats = @{ dirs = 0; denied = 0; big = 0; pdf = 0 }
function Get-Files([string]$root) {
    $stack = New-Object System.Collections.Generic.Stack[string]
    $stack.Push($root)
    while ($stack.Count) {
        $dir = $stack.Pop()
        $stats.dirs++
        try { $info = New-Object System.IO.DirectoryInfo($dir); $items = $info.GetFileSystemInfos() }
        catch { $stats.denied++; Log "Нет доступа к папке: $dir" -Quiet; continue }
        foreach ($it in $items) {
            if ($it -is [System.IO.DirectoryInfo]) {
                if (($it.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -or $SkipFolders -contains $it.Name) { continue }
                $stack.Push($it.FullName)
            } else { $it }
        }
    }
}

$extensions = $Include | ForEach-Object { '.' + $_.TrimStart('.').ToLower() }
$found = New-Object System.Collections.Generic.List[object]
$unread = New-Object System.Collections.Generic.List[string]
$seen = 0
foreach ($root in $Path) {
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { Log "Папка не найдена или нет доступа: $root"; continue }
    foreach ($f in (Get-Files $root)) {
        $ext = $f.Extension.TrimStart('.').ToLower()
        if ($ext -eq 'pdf') { $stats.pdf++ }
        if ($extensions -notcontains $f.Extension.ToLower() -or $f.Name -like '~$*') { continue }
        if ($f.Length -gt $MaxSizeMB * 1MB) { $stats.big++; continue }
        $seen++
        if ($seen % 20 -eq 0) { Write-Progress -Activity "Поиск в файлах ($seen просмотрено, $($found.Count) найдено)" -Status $f.DirectoryName }
        try {
            if ($zipParts.ContainsKey($ext)) { $text = Read-ZipText $f.FullName $zipParts[$ext] }
            elseif ($binary -contains $ext) { $text = Read-BinaryText $f.FullName }
            elseif ($ext -eq 'rtf') { $text = Read-RtfText $f.FullName }
            else { $text = Read-PlainText $f.FullName }
        } catch {
            $unread.Add($f.FullName)
            Log ("Не прочитан: {0} — {1}" -f $f.FullName, $_.Exception.Message) -Quiet
            continue
        }
        $hits = @()
        $words = @()
        for ($k = 0; $k -lt $regexes.Count; $k++) {
            $m = $regexes[$k].Match($text)
            if ($m.Success) { $hits += $m; $words += $Pattern[$k] }
        }
        if ($hits.Count -eq 0 -or ($All -and $hits.Count -lt $regexes.Count)) { continue }

        $first = $hits[0]
        $start = [Math]::Max(0, $first.Index - 60)
        $end = [Math]::Min($text.Length, $start + 160)
        if ($binary -contains $ext) {            # старые форматы: только кусок текста с совпадением, без служебных байтов
            $start = [Math]::Max($start, $text.LastIndexOf("`n", $first.Index) + 1)
            $nl = $text.IndexOf("`n", $first.Index)
            if ($nl -ge 0) { $end = [Math]::Min($end, $nl) }
        }
        $snippet = $text.Substring($start, $end - $start) -replace '\s+', ' '
        $found.Add([pscustomobject]@{
            'Файл'       = $f.FullName
            'Тип'        = $ext
            'Изменён'    = $f.LastWriteTime.ToString('dd.MM.yyyy HH:mm')
            'Размер, КБ' = [Math]::Ceiling($f.Length / 1KB)
            'Слова'      = $words -join ', '
            'Совпадений' = [int]($regexes | ForEach-Object { $_.Matches($text).Count } | Measure-Object -Sum).Sum
            'Фрагмент'   = $snippet.Trim()
        })
    }
}
Write-Progress -Activity "Поиск в файлах" -Completed

Log ("Просмотрено файлов: {0}, найдено: {1}" -f $seen, $found.Count)
if ($unread.Count) { Log ("Не прочитаны (повреждены или защищены паролем): {0} — список в журнале {1}" -f $unread.Count, $log) }
if ($stats.denied) { Log ("Папок без доступа: {0} — пропущены" -f $stats.denied) }
if ($stats.big) { Log ("Больше {0} МБ: {1} — пропущены (увеличить: -MaxSizeMB 200)" -f $MaxSizeMB, $stats.big) }
if ($stats.pdf) {
    Log ("PDF в папках: {0} — их скрипт не читает. Найти в них: Проводник → поле поиска → содержимое:слово, или прикрепить нужные PDF в чат помощнику" -f $stats.pdf)
}
if ($found.Count -and -not $NoCsv) {
    if (-not $OutCsv) {
        $OutCsv = Join-Path ([Environment]::GetFolderPath('Desktop')) ('Поиск_{0:yyyy-MM-dd_HHmm}.csv' -f (Get-Date))
    }
    $enc = 'UTF8'; if ($PSVersionTable.PSVersion.Major -ge 6) { $enc = 'utf8BOM' }
    $found | Sort-Object 'Совпадений' -Descending |
        Export-Csv -LiteralPath $OutCsv -NoTypeInformation -Delimiter ';' -Encoding $enc
    Log "Список сохранён: $OutCsv (открывается в Excel двойным щелчком)"
}
$found | Sort-Object 'Совпадений' -Descending | Select-Object 'Файл', 'Слова', 'Совпадений', 'Фрагмент' |
    Format-Table -AutoSize -Wrap
