from datetime import datetime
import luigi
from luigi.contrib.kubernetes import KubernetesJobTask
import pandas as pd
from common import MSSqlConnection, ADLTarget, ADLFlagTarget, course_week
from pipeline.query_data import CourseDatesQueryTask
from pipeline import Pipeline


def pipeline_runner():
    def _decorator(_class):
        _class.task_namespace = 'runner'
        return _class
    return _decorator


@pipeline_runner()
class CourseDatesTask(luigi.Task):
    course_id = luigi.Parameter()

    def requires(self):
        return CourseDatesQueryTask(self.course_id)
    
    def output(self):
        return self.input()


@pipeline_runner()
class PipelineRunner(luigi.Task):
    """
    Main Pipeline class that uses kubernetes cluster to orchestrate 
    pipeline jobs for each course.
    """

    local = luigi.BoolParameter()

    def requires(self):
        if self.local:
            return EdxTopCourseIdsTask()
        else:
            return EdxCourseIdsTask()

    def output(self):
        current_date_string = datetime.strftime(datetime.today(), '%Y-%m-%d')
        return ADLFlagTarget('data/pipeline/{}/'.format(current_date_string))

    def run(self):
        edx_course_ids = list()
        with self.input().open() as course_ids_file:
            if self.local:
                for course_id in course_ids_file:
                    edx_course_ids.append(course_id.strip())
            else:
                edx_course_ids = pd.read_csv(course_ids_file)
        
        if self.local:
            yield [SingleCourseLocalTask(course_id) for course_id in edx_course_ids]
            # yield SingleCourseLocalTask('Micros oft+DAT222x+4T2017')
        else:
            # yield [SingleCourseKubernetesJobTask(course_id) for course_id in list(edx_course_ids)] 
            yield SingleCourseKubernetesJobTask('Microsoft+DAT222x+4T2017')

        with self.output().open('w') as output:
            output.write('')


@pipeline_runner()
class SingleCourseLocalTask(luigi.Task):

    course_id = luigi.Parameter()

    def requires(self):
        return {
            'course_dates': CourseDatesTask(self.course_id)
        }

    def output(self):
        self.course_start_date, _ = self._get_course_dates()
        self.current_course_week = course_week(datetime.utcnow(), self.course_start_date)
        self.success_dir_path = 'data/{}/week_{}/'.format(self.course_id, self.current_course_week)
        return ADLFlagTarget(self.success_dir_path)

    def run(self):
        course_start_date, _ = self._get_course_dates()
        current_course_week = course_week(datetime.utcnow(), course_start_date)
        success_dir_path = 'data/{}/week_{}/'.format(self.course_id, current_course_week)

        print('BEFORE PIPELINE RUN')
        print(self.output())

        yield Pipeline(path=success_dir_path, course_id=self.course_id, course_start_date=course_start_date.date(), current_course_week=current_course_week)

        print('AFTER PIPELINE RUN')
        print(self.output())

        with self.output().open('w') as output:
            output.write('')

    def _get_course_dates(self):
        with self.input().get('course_dates').open() as course_dates_file:
            course_dates = pd.read_csv(course_dates_file)
            def get_datetime_col(col_name):
                """
                Get column as a datetime object
                """
                return datetime.strptime(course_dates[col_name][0], '%Y-%m-%d')

            course_start_date = get_datetime_col('CourseRunStartDate')
            course_end_date = get_datetime_col('CourseRunEndDate')
            return (course_start_date, course_end_date)


@pipeline_runner()
class SingleCourseKubernetesJobTask(KubernetesJobTask):
    
    course_id = luigi.Parameter()

    name = 'conductor'
    spec_schema = {
        "containers": [{
            "name": "single-course",
            "image": "learnerattrition.azurecr.io/learner-attrition-pipeline"
        }],
        "imagePullSecrets": [{
            "name": "registrykey"
        }]
    }

    def requires(self):
        return {
            'edx_course_ids': EdxCourseIdsTask(),
            'course_dates': CourseDatesTask(self.course_id)
        }

    def output(self):
        self.course_start_date, _ = self._get_course_dates()
        self.current_course_week = course_week(datetime.utcnow(), self.course_start_date)
        self.success_dir_path = 'data/{}/week_{}/'.format(self.course_id, self.current_course_week)
        return ADLFlagTarget(self.success_dir_path)

    def run(self):
        course_start_date, _ = self._get_course_dates()
        current_course_week = course_week(datetime.utcnow(), course_start_date)
        success_dir_path = 'data/{}/week_{}/'.format(self.course_id, current_course_week)
        self.spec_schema["containers"][0]["args"] = [
            self.course_id, str(current_course_week), course_start_date.strftime('%Y-%m-%d'), success_dir_path
        ]

        print('=====================================================')
        print('=====================================================')
        print(self.spec_schema)
        print('=====================================================')
        print('=====================================================')
        
        super().run()
        # print(self.spec_schema)

    def signal_complete(self):
        with self.output().open('w') as output:
            output.write('')

    def _get_course_dates(self):
        with self.input().get('course_dates').open() as course_dates_file:
            course_dates = pd.read_csv(course_dates_file)
            def get_datetime_col(col_name):
                """
                Get column as a datetime object
                """
                return datetime.strptime(course_dates[col_name][0], '%Y-%m-%d')

            course_start_date = get_datetime_col('CourseRunStartDate')
            course_end_date = get_datetime_col('CourseRunEndDate')
            return (course_start_date, course_end_date)
    

@pipeline_runner()
class AllCourseDatesTask(luigi.WrapperTask):
    def requires(self):
        course_ids = get_top_course_ids()
        print(course_ids)
        for course_id in course_ids:
            yield CourseDatesTask(course_id)


@pipeline_runner()
class EdxCourseIdsTask(luigi.Task):
    """
    Get all edx course ids to run pipelines for
    """
    _current_date_string = datetime.strftime(datetime.today(), '%Y-%m-%d')
    _query = """
        SELECT DISTINCT CourseRunId
        FROM [EdxDW].[edx].[DimCourse] C
        WHERE [CourseRunStartDate] < '{current_date}'
        AND [CourseRunEndDate] > '{current_date}'
    """

    def output(self):
        return ADLTarget("data/edx_course_ids_%s.csv" % self._current_date_string, overwrite=True)

    def run(self):
        conn = MSSqlConnection()
        edx_course_ids = conn.run_query(self._query.format(current_date=self._current_date_string))
        edx_course_ids.columns = ['edx_course_id']
        
        with self.output().open('w') as output:
            edx_course_ids.to_csv(output, index=False)

        return list(edx_course_ids['edx_course_id'])


@pipeline_runner()
class EdxTopCourseIdsTask(luigi.ExternalTask):
    """
    Top course ids file
    """
    def output(self):
        return luigi.LocalTarget('/home/kabirkhan/Documents/ML_Experiments/1_edx_learner_attrition/edx-learner-attrition/data/top_course_ids.txt')

def get_top_course_ids():
    edx_course_ids = list()
    with open('/home/kabirkhan/Documents/ML_Experiments/1_edx_learner_attrition/edx-learner-attrition/data/top_course_ids.txt') as top_course_ids:
        for course_id in top_course_ids:    
            edx_course_ids.append(course_id.strip())

    return edx_course_ids


if __name__ == "__main__":
    luigi.run()
