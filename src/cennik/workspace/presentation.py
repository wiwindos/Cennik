from fastapi.responses import FileResponse, HTMLResponse


def page(path, request):
    prefix = getattr(request.state, "cennik_mount", "")
    if not prefix:
        return FileResponse(path)
    text = path.read_text("utf-8").replace('href="/', 'href="' + prefix + '/').replace('src="/', 'src="' + prefix + '/')
    return HTMLResponse(text)
