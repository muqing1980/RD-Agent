import pandas as pd

from rdagent.app.data_science.conf import DS_RD_SETTING
from rdagent.components.coder import CoSTEER
from rdagent.components.coder.CoSTEER import CoSTEER
from rdagent.components.coder.CoSTEER.config import CoSTEER_SETTINGS
from rdagent.components.coder.CoSTEER.evaluators import (
    CoSTEERMultiEvaluator,
    CoSTEERSingleFeedback,
)
from rdagent.components.coder.CoSTEER.evolvable_subjects import FBWorkspace
from rdagent.components.coder.CoSTEER.evolving_strategy import (
    CoSTEERQueriedKnowledge,
    MultiProcessEvolvingStrategy,
)
from rdagent.components.coder.CoSTEER.task import CoSTEERTask
from rdagent.components.coder.data_science.conf import DSCoderCoSTEERSettings
from rdagent.components.coder.data_science.share.eval import ModelDumpEvaluator
from rdagent.core.exception import RunnerError
from rdagent.core.scenario import Scenario
from rdagent.log import rdagent_logger as logger
from rdagent.oai.llm_utils import APIBackend, md5_hash
from rdagent.scenarios.data_science.dev.runner.eval import DSCoSTEERCoSTEEREvaluator
from rdagent.utils.agent.ret import PythonBatchEditOut, PythonBatchPatchOut
from rdagent.utils.agent.tpl import T
from rdagent.utils.workflow import wait_retry


class DSRunnerCoSTEERSettings(DSCoderCoSTEERSettings):
    """Data Science CoSTEER settings"""

    class Config:
        env_prefix = "DS_Runner_CoSTEER_"

    max_seconds: int = DS_RD_SETTING.full_timeout
    env_type: str = "docker"
    diff_mode: bool = False
    # TODO: extract a function for env and conf.


class DSRunnerMultiProcessEvolvingStrategy(MultiProcessEvolvingStrategy):
    @wait_retry(retry_n=5)
    def implement_one_task(
        self,
        target_task: CoSTEERTask,
        queried_knowledge: CoSTEERQueriedKnowledge | None = None,
        workspace: FBWorkspace | None = None,
        prev_task_feedback: CoSTEERSingleFeedback | None = None,
    ) -> dict[str, str]:

        if prev_task_feedback is None:
            # if no prev_task_feedback, it is the first loop; we do not make any changes and goto evaluators directly.
            return {}

        # Output Agent Map
        output_map = {
            True: (PythonBatchPatchOut.get_spec(), PythonBatchPatchOut.extract_output),
            False: (
                PythonBatchEditOut.get_spec(with_del=False),
                PythonBatchEditOut.extract_output,
            ),
        }
        output_spec, extract_output_fn = output_map[self.settings.diff_mode]

        if prev_task_feedback.hyperparameter_tuning_decision:
            # Use system_refine for hyperparameter tuning
            system_prompt = T(".prompts:DSCoSTEER.system_refine").r(
                out_spec=output_spec,
                diff_mode=self.settings.diff_mode,
            )
        else:
            task_information_str = target_task.get_task_information()
            # Use system_debugger for error fixing and debugging
            system_prompt = T(".prompts:DSCoSTEER.system_refine").r(
                task_desc=task_information_str,
                out_spec=output_spec,
                diff_mode=self.settings.diff_mode,
            )

        # Generate user prompt for both cases
        user_prompt = T(".prompts:DSCoSTEER.user").r(
            code=workspace.all_codes,
            feedback=prev_task_feedback,
            hyperparameter_tuning_suggestion=prev_task_feedback.hyperparameter_tuning_suggestion,
        )

        batch_edit = extract_output_fn(
            APIBackend().build_messages_and_create_chat_completion(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
            )
        )

        batch_edit = {k: v for k, v in batch_edit.items() if k in workspace.file_dict.keys()}

        return batch_edit

    def assign_code_list_to_evo(self, code_list: list[dict[str, str]], evo):
        """
        Assign the code list to the evolving item.

        The code list is aligned with the evolving item's sub-tasks.
        If a task is not implemented, put a None in the list.
        """
        for index in range(len(evo.sub_tasks)):
            if code_list[index] is None:
                continue
            if evo.sub_workspace_list[index] is None:
                # evo.sub_workspace_list[index] = FBWorkspace(target_task=evo.sub_tasks[index])
                evo.sub_workspace_list[index] = evo.experiment_workspace
            evo.sub_workspace_list[index].inject_files(**code_list[index])
        return evo


class DSCoSTEERRunner(CoSTEER):
    def __init__(
        self,
        scen: Scenario,
        *args,
        **kwargs,
    ) -> None:

        eval_l = [DSCoSTEERCoSTEEREvaluator(scen=scen)]
        if DS_RD_SETTING.enable_model_dump:
            eval_l.append(ModelDumpEvaluator(scen=scen, data_type="full"))

        eva = CoSTEERMultiEvaluator(
            single_evaluator=eval_l, scen=scen
        )  # Please specify whether you agree running your eva in parallel or not
        settings = DSRunnerCoSTEERSettings()
        es = DSRunnerMultiProcessEvolvingStrategy(scen=scen, settings=settings)

        # In runner, we don't need very big loops, so we set max_loop to runner_max_loop
        super().__init__(
            *args,
            settings=settings,
            eva=eva,
            es=es,
            evolving_version=2,
            scen=scen,
            max_loop=DS_RD_SETTING.runner_max_loop,
            **kwargs,
        )

    def develop(self, exp):
        bak_sub_tasks = exp.sub_tasks
        exp.sub_tasks = [
            CoSTEERTask(
                name="Debug running solution",
                description=f"You'll be provided with the source code and the running and testing stdout. "
                "Please check the error messages and debug the source code if any errors occur.\n"
                f"Current code repo md5: {md5_hash(exp.experiment_workspace.all_codes)}",
            ),
        ]
        exp = super().develop(exp)  # run strategy(code implementation & evaluation loops)
        exp.sub_tasks = bak_sub_tasks

        # NOTE: after running the loops, we expect some results are generated
        #
        # 1) scores of the models and ensemble
        score_fp = exp.experiment_workspace.workspace_path / "scores.csv"
        if not score_fp.exists():
            logger.error("Metrics file (scores.csv) is not generated.")
            raise RunnerError(f"Metrics file (scores.csv) is not generated")
        exp.result = pd.read_csv(score_fp, index_col=0)
        exp.running_info.running_time = exp.experiment_workspace.running_info.running_time

        # 2) if mle-bench, then the submission format checking will be used.
        # DockerEnv for MLEBench submission validation
        if DS_RD_SETTING.if_using_mle_data:
            score_fp = exp.experiment_workspace.workspace_path / "test" / "mle_submission_format_test.output"
            with score_fp.open() as f:
                exp.format_check_result = f.read()
        return exp
