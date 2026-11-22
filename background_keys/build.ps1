$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) {
    throw 'Не найден компилятор .NET Framework 4.x.'
}
$output = Join-Path $PSScriptRoot 'BackgroundKeys.exe'
$sources = @((Join-Path $PSScriptRoot 'BackgroundKeys.cs'), (Join-Path $PSScriptRoot 'SelfTest.cs'))
& $compiler /nologo /target:winexe /platform:anycpu /optimize+ /codepage:65001 "/out:$output" /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll $sources
if ($LASTEXITCODE -ne 0) { throw "Ошибка сборки: $LASTEXITCODE" }
Write-Output "Готово: $output"
# EOF
