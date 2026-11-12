"""Deterministic pre-clock readiness; no model client or credential is loaded."""
import time
from types import SimpleNamespace
from .deployment import require_compute, check_fixture_namespace, DeploymentRefused
from .owned import run_owned
from .sandbox import command


def prepare(controller):
    require_compute()
    root=controller.root/'readiness';root.mkdir()
    config=SimpleNamespace(run=root/'run',control=root/'control',work=root/'work',state=root/'state',
        role='research',lane='dsf',model=controller.policy['models']['dsf']['model_id'],
        scenario='normal',release=controller.release,seconds=180,
        fixtures=controller.deployment['fixtures'],deployment_mode=controller.deployment['mode'])
    config.problem_mount=controller.deployment.get('problem')
    config.research_live=bool(getattr(controller,'live',False))
    config.preflight_only=True
    for path in (config.run,config.control,config.work,config.state):path.mkdir()
    from .startup_log import phase
    phase('readiness: building sandbox command')
    cmd,env=command(config)
    phase('readiness: tool checks')
    check_fixture_namespace(cmd,env,config.fixtures)
    if config.problem_mount:
        check_fixture_namespace(cmd,env,[config.problem_mount,
            dict(config.problem_mount,target='/work/PROBLEM.md')])
    prefix=cmd[:cmd.index('/bin/bash')]
    (config.work/'readiness.gp').write_text('print("READY_GP"); quit;\n')
    checks=[(['sage','-c','R=PolynomialRing(QQ,names=("x","y")); x,y=R.gens(); assert R.ideal([x*x-1,y-x]).dimension()==0; print("READY_SAGE")'],'READY_SAGE'),
            (['gap','--nointeract','-q','-c','if Size(SymmetricGroup(5))<>120 then Error("readiness"); fi; Print("READY_GAP\\n");'],'READY_GAP'),
            (['gp','-fq','/work/readiness.gp'],'READY_GP')]
    for argv,marker in checks:
        result=run_owned(prefix+argv,env=env,capture_output=True,timeout=180)
        if result.returncode or marker not in result.stdout:
            raise DeploymentRefused('pre-clock semantic tool readiness failed: '+argv[0])
    fallback_ids={}
    for arm in sorted(controller.streams):
        parent=controller.root/'arms'/arm;parent.mkdir(parents=True)
        identity='preclock-fallback-'+arm
        extra={}
        if arm.endswith('-swarm'):
            from .swarm_runtime import assembler_id
            extra['assembler_id']=assembler_id(arm,controller.cohort)
        controller.publisher.submit_operation(identity,'preflight',dict(
            catalog=dict(root=str(parent/'closure-state'),arm_id=arm,problem_id=controller.problem,
                strategy=arm,profile=controller.profile,artifact_root=str(controller.durable/arm/'closure'),**extra),
            scratch=str(controller.root)))
        deadline=time.monotonic()+180
        while True:
            result=controller.publisher.operation_result(identity)
            if result is not None:break
            if time.monotonic()>=deadline:raise DeploymentRefused('pre-clock fallback acknowledgment timed out')
            time.sleep(.02)
        fallback_ids[arm]=result['fallback_bundle']
    return dict(ready_at=controller.clock(),tools=['sage','gap','gp'],fallbacks=fallback_ids,
                fixture_mounts_checked=bool(config.fixtures),model_requests=0)
