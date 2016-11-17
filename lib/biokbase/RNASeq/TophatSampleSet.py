import simplejson, sys, shutil, os, ast , re
from mpipe import OrderedStage , Pipeline
import glob, json, uuid, logging  , time ,datetime 
import subprocess, threading,traceback
from collections import OrderedDict
from pprint import pprint , pformat
import parallel_tools as parallel
from mpipe import OrderedStage , Pipeline
import contig_id_mapping as c_mapping 
import script_util
import handler_utils as handler_util
from biokbase.workspace.client import Workspace
from biokbase.auth import Token
import multiprocessing as mp
from biokbase.RNASeq import rnaseq_util
import doekbase.data_api
from doekbase.data_api.annotation.genome_annotation.api import GenomeAnnotationAPI , GenomeAnnotationClientAPI
from doekbase.data_api.sequence.assembly.api import AssemblyAPI , AssemblyClientAPI
import requests.packages.urllib3
requests.packages.urllib3.disable_warnings()
try:
    from biokbase.HandleService.Client import HandleService
except:
    from biokbase.AbstractHandle.Client import AbstractHandle as HandleService
from biokbase.RNASeq.Tophat import Tophat
from AssemblyUtil.AssemblyUtilClient import AssemblyUtil
from GenomeFileUtil.GenomeFileUtilClient import GenomeFileUtil
#import ExecutionBase.ExecutionBase as ExecutionBase

class TophatSampleSetException(Exception):
    pass

class TophatSampleSet(Tophat): 

    def __init__(self, logger, directory, urls):
        super(TophatSampleSet, self).__init__(logger, directory, urls)

        # user defined shared variables across methods
        self.sample = None
        self.bowtie2index_id = None
        #self.num_threads = None


    def prepare( self, common_params, method_params ): 
        # for quick testing, we recover parameters here
        print( "in TophatSampleSet.prepare(), common_params are ")
        pprint( common_params )
        print( " and method_params are" )
        pprint( method_params )
        ws_client = common_params['ws_client']
        hs = common_params['hs_client']
        params = method_params
        logger = self.logger
        token = common_params['user_token']
        tophat_dir = self.directory             # QUESTION: Will this be shared directory if local?

        try:
               sample, bowtie_index = ws_client.get_objects(
                                        [ { 'name' : params['sampleset_id'], 'workspace' : params['ws_id'] },
                                          { 'name' : params['bowtie_index'], 'workspace' : params['ws_id'] }
                                        ] )
               self.sample = sample
        except Exception,e:
               logger.exception("".join(traceback.format_exc()))
               raise ValueError(" Error Downloading objects from the workspace ")
        ### Get object Info and IDs
        sample_info = ws_client.get_object_info_new({"objects": [{'name': params['sampleset_id'], 'workspace': params['ws_id']}]})[0]
        sample_type = sample_info[2].split('-')[0]

        # SampleSet
        reads = sample['data']['sample_ids']
        reads_type= sample['data']['Library_type']
        if reads_type == 'PairedEnd': r_type = 'KBaseAssembly.PairedEndLibrary'
        else: r_type = 'KBaseAssembly.SingleEndLibrary'
        e_ws_objs = script_util.if_ws_obj_exists(None,ws_client,params['ws_id'],r_type,reads)
        missing_objs = [i for i in reads if not i in e_ws_objs]
        if len(e_ws_objs) != len(reads):
           raise ValueError('Missing Library objects {0} in the {1}. please copy them and run this method'.format(",".join(missing_objs),params['ws_id']))


        ### Get object IDs
        bowtie2_index_info,sampleset_info = ws_client.get_object_info_new({"objects": [{'name': params['bowtie_index'], 'workspace': params['ws_id']},{'name': params['sampleset_id'], 'workspace': params['ws_id']}]})
        self.bowtie2index_id = str(bowtie2_index_info[6]) + '/' + str(bowtie2_index_info[0]) + '/' + str(bowtie2_index_info[4])  
        sampleset_id = str(sampleset_info[6]) + '/' + str(sampleset_info[0]) + '/' + str(sampleset_info[4]) 
        bw_id = bowtie_index['data']['handle']['id'] 
        bw_name =  bowtie_index['data']['handle']['file_name']
        genome_id = bowtie_index['data']['genome_id']
        annotation_gtf = ws_client.get_object_info([{"ref" :genome_id}],includeMetadata=None)[0][1]
        shared_files={}
        shared_files[bw_name] = bw_id
        script_util.download_shock_files(logger,self.urls['shock_service_url'],tophat_dir,shared_files,token)
        try:
            logger.info("Unzipping Bowtie2 Indices")
            script_util.unzip_files(logger,os.path.join(tophat_dir,bw_name),tophat_dir)
            mv_dir= handler_util.get_dir(tophat_dir)
            if mv_dir is not None:
                    script_util.move_files(logger,mv_dir,tophat_dir)
        except Exception, e:
               logger.error("".join(traceback.format_exc()))
               raise Exception("Unzip indexfile error: Please contact help@kbase.us")
        fasta_file =os.path.join(tophat_dir,(handler_util.get_file_with_suffix(tophat_dir,".fa")+".fa"))
        bowtie2base =os.path.join(tophat_dir,handler_util.get_file_with_suffix(tophat_dir,".rev.1.bt2"))

        ### Check if GTF annotation object exist or skip this step
        ### Check if the gtf object exists in the workspace
        ### Only run create_gtf_annotation if object doesnt exist
        ws_gtf = annotation_gtf+"_GTF_Annotation"
        ret = script_util.if_obj_exists(None,ws_client,params['ws_id'],"KBaseRNASeq.GFFAnnotation",[ws_gtf])
        if not ret is None:
            logger.info("GFF Annotation Exist for Genome Annotation {0}.... Skipping step ".format(annotation_gtf))
            annot_name,annot_id = ret[0]
            gtf_obj=ws_client.get_objects([{'ref' : annot_id}])[0]
            gtf_id=gtf_obj['data']['handle']['id']
            gtf_name=gtf_obj['data']['handle']['file_name']
            try:
               script_util.download_file_from_shock(logger, shock_service_url=self.urls['shock_service_url'], shock_id=gtf_id,filename=gtf_name, directory=tophat_dir,token=token)
               gtf_file = os.path.join(tophat_dir,gtf_name)
            except Exception,e:
               logger.exception(e)
               raise Exception( "Unable to download shock file, {0}".format(gtf_name))  
        else:
            gtf_file =rnaseq_util.create_gtf_annotation_from_genome(logger,ws_client,hs,self.urls,params['ws_id'],genome_id,annotation_gtf,tophat_dir,token)
        # Determine the num_threads provided by the user otherwise default the number of threads to 2
        reads = sample['data']['sample_ids']
        reads_type= sample['data']['Library_type']
        r_label = sample['data']['condition']
        self.num_jobs =  len(reads)

        count = 0
        logger.info(" Number of threads used by each process {0}".format(self.num_threads))
        for i in reads:
            try:
                    label = r_label[count]
                    task_param = {'input_arguments' :  
                                  [
                                   {'job_id' : i,
                                    'label' : r_label[count],
                                    'ws_id' : params['ws_id'],
                                    'reads_type' : reads_type,
                                    'tophat_dir' : self.directory,
                                    'gtf_file' : gtf_file,
                                    'annotation_id': genome_id,
                                    'sampleset_id' : sampleset_id,
                                    'bowtie_index' : params['bowtie_index'],
                                    'bowtie2index_id' : self.bowtie2index_id
                                   }
                                  ]
                                 }
                    self.task_list.append(task_param)
                    count = count + 1
            except Exception,e:
                    raise

        return self.task_list


    def collect(self, common_params, method_params):
        # do with 
        print( "in TophatSampleSet.collect(), common_params are ")
        pprint( common_params )
        print( " and method_params are" )
        pprint( method_params )

        global_params = method_params['global_params']
        input_result_pairs = method_params['input_result_pairs']
        single_alignment = input_result_pairs[0]['output_name']

        sref = common_params['ws_client'].get_object_info_new({"objects": [{'name':single_alignment, 'workspace': global_params['ws_id']}]})[0]
        returnVal = { 'output': single_alignment, 'workspace': global_params['ws_id'] }
        return( returnVal )

        alignmentSet_name = global_params['sampleset_id']+"_tophat_AlignmentSet"
        self.logger.info(" Creating AlignmentSet for the Alignments {0}".format(alignmentSet_name))
        # TODO: Split alignment set and report method
        #reportObj=rnaseq_util.create_RNASeq_AlignmentSet_and_build_report( self.logger, 
        #                                                                   common_params['ws_client'],
        #                                                                   method_params['ws_id'],sample['data']['sample_ids'],
        #                                                                   self.task_list[0]['sampleset_id'],
        #                                                                   self.task_list[0]['annotation_id'],
        #                                                                   self.bowtie2index_id,
        #                                                                   self.results,
        #                                                                   alignmentSet_name)
        returnVal = { 'output'  : alignmentSet_name ,'workspace' : global_params['ws_id']}
        return( returnVal )
#	reportName = 'Align_Reads_using_Tophat_'+str(hex(uuid.getnode()))
#        report_info = self.common_params['ws_client'].save_objects({
#                                                'workspace':self.method_params['ws_id'],
#                                                'objects':[
#                                                {
#                                                'type':'KBaseReport.Report',
#                                                'data':reportObj,
#                                                'name':reportName,
#                                                'meta':{},
#                                                'hidden':1, # important!  make sure the report is hidden
#                                                #'provenance':provenance
#                                                }
#                                                ]
#                                                })[0]
#
#        self.returnVal = { "report_name" : reportName,"report_ref" : str(report_info[6]) + '/' + str(report_info[0]) + '/' + str(report_info[4]) }
#
