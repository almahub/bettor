"""Share an in-flight read without cancelling paid work when a caller disconnects."""
import asyncio


class SingleFlight:
    def __init__(self):
        self.tasks = {}

    async def run(self, key, operation):
        task = self.tasks.get(key)
        shared = task is not None
        if task is None:
            task = asyncio.create_task(operation())
            self.tasks[key] = task

            def finished(done):
                if self.tasks.get(key) is done:
                    self.tasks.pop(key, None)
                if not done.cancelled():
                    done.exception()  # Observe errors even if all callers disconnected.

            task.add_done_callback(finished)
        result = await asyncio.shield(task)
        return result.model_copy(update={'cached': True}) if shared and result is not None else result
