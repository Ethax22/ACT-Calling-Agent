"""The tools the model can call on a phone call, and what each one does.

Same job as the Indus tools: the records tools return erp-lookup's ready-to-speak
`speech` for the model to say as it is. Caller identification is not a tool here: it
runs once before the greeting (call_pipeline.py) and the role reaches the backend as a
header. Transfer is one tool, not Indus's two: the model gives the department and the
one line to say, and we say it, then hand the call over.
"""
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

TOOLS = ToolsSchema(standard_tools=[
    FunctionSchema(
        name="get_student_report",
        description="Marks or attendance for a student, for a verified registered student or parent caller. "
                    "Only call it after the caller has given the register number and date of birth.",
        properties={
            "reg_no": {"type": "string", "description": "The student's register number, digits only."},
            "date_of_birth": {"type": "string", "description": "The student's date of birth as DD-MM-YYYY."},
            "topic": {"type": "string", "enum": ["marks", "attendance", "both"]},
            "subject": {"type": "string", "description": "One subject if the caller asked about one, else empty."},
        },
        required=["reg_no", "date_of_birth", "topic"],
    ),
    FunctionSchema(
        name="get_class_summary",
        description="A faculty caller's class summary: students below 75 percent attendance, or the latest test result.",
        properties={"topic": {"type": "string", "enum": ["low_attendance", "marks_summary"]}},
        required=["topic"],
    ),
    FunctionSchema(
        name="transfer_call",
        description="Hand the call to a person. Give the department and the one short line to say first.",
        properties={
            "department": {"type": "string", "enum": ["reception", "admissions", "placement"]},
            "line": {"type": "string", "description": "One short sentence in the caller's language, e.g. 'Connecting you to the admissions team now.'"},
        },
        required=["department", "line"],
    ),
    FunctionSchema(
        name="end_call",
        description="End the call after the caller is done. Give the short goodbye to say.",
        properties={"goodbye": {"type": "string", "description": "A goodbye of under ten words in the caller's language."}},
        required=["goodbye"],
    ),
])


def make_handlers(erp, control, caller_number: str) -> dict:
    async def get_student_report(params: FunctionCallParams):
        a = params.arguments
        logger.info(f"[tool] get_student_report topic={a.get('topic')} subject={a.get('subject')!r}")  # no register number or DOB in logs
        await params.result_callback(await erp.student(
            caller_number, str(a.get("reg_no", "")), str(a.get("date_of_birth", "")),
            str(a.get("topic", "both")), str(a.get("subject", ""))))

    async def get_class_summary(params: FunctionCallParams):
        logger.info(f"[tool] get_class_summary topic={params.arguments.get('topic')}")
        await params.result_callback(await erp.class_summary(caller_number, str(params.arguments.get("topic", ""))))

    async def transfer_call(params: FunctionCallParams):
        ok = await control.transfer(str(params.arguments.get("department", "reception")),
                                    str(params.arguments.get("line", "")))
        if ok:
            from pipecat.frames.frames import FunctionCallResultProperties
            await params.result_callback({"status": "ok"}, properties=FunctionCallResultProperties(run_llm=False))
        else:  # no number to dial: the model apologises and offers a callback
            await params.result_callback({"status": "error", "speech": "No transfer number is available right now."})

    async def end_call(params: FunctionCallParams):
        from pipecat.frames.frames import FunctionCallResultProperties
        await params.result_callback({"status": "ok"}, properties=FunctionCallResultProperties(run_llm=False))
        await control.say_and_end(str(params.arguments.get("goodbye", "")), "end_call")

    return {"get_student_report": get_student_report, "get_class_summary": get_class_summary,
            "transfer_call": transfer_call, "end_call": end_call}
