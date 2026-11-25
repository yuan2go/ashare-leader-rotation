from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..schemas import Account, Ticket
from ..services.shadow_service import ticket_rows

router = APIRouter(prefix="/tickets",tags=["Tickets"])


@router.get("/next-open",response_model=list[Ticket],operation_id="getNextOpenTickets",summary="次日开盘影子指令试算")
def next_open(request: Request, account: Account | None = None):
    return [row for row in ticket_rows(request.app.state.settings) if account is None or row["account"]==account]


def export(settings, suffix, media):
    name = f"next_open_order_ticket.{suffix}"
    return Response((settings.shadow_dir/name).read_bytes(),media_type=media,
                    headers={"Content-Disposition":f'attachment; filename="{name}"'})


class CsvResponse(Response):
    media_type = "text/csv"


class MarkdownResponse(Response):
    media_type = "text/markdown"


@router.get("/next-open.csv",response_class=CsvResponse,operation_id="downloadNextOpenCsv",summary="下载指令单 CSV")
def csv_export(request: Request):
    return export(request.app.state.settings,"csv","text/csv")


@router.get("/next-open.md",response_class=MarkdownResponse,operation_id="downloadNextOpenMarkdown",summary="下载指令单 Markdown")
def markdown_export(request: Request):
    return export(request.app.state.settings,"md","text/markdown")
