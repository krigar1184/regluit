import re
import sys
import json
import logging
import urllib

from datetime import timedelta, date
from regluit.utils.localdatetime import now, date_today

from random import randint
from re import sub
from itertools import islice
from decimal import Decimal as D
from xml.etree import ElementTree as ET
import requests
import oauth2 as oauth
from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.core import signing
from django.core.urlresolvers import reverse
from django.core.exceptions import ObjectDoesNotExist
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.comments import Comment
from django.contrib.sites.models import Site
from django.db.models import Q, Count, Sum
from django.forms import Select
from django.forms.models import modelformset_factory
from django.http import HttpResponseRedirect, Http404
from django.http import HttpResponse, HttpResponseNotFound
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic.edit import FormView
from django.views.generic.list import ListView
from django.views.generic.base import TemplateView
from django.shortcuts import render, render_to_response, get_object_or_404
from django.utils.http import urlencode
from django.utils.translation import ugettext_lazy as _
from regluit.core import tasks
from regluit.core.tasks import send_mail_task, emit_notifications
from regluit.core import models, bookloader, librarything
from regluit.core import userlists
from regluit.core.search import gluejar_search
from regluit.core.goodreads import GoodreadsClient
from regluit.frontend.forms import UserData, UserEmail, ProfileForm, CampaignPledgeForm, GoodreadsShelfLoadingForm
from regluit.frontend.forms import  RightsHolderForm, UserClaimForm, LibraryThingForm, OpenCampaignForm
from regluit.frontend.forms import getManageCampaignForm, DonateForm, CampaignAdminForm, EmailShareForm, FeedbackForm
from regluit.frontend.forms import EbookForm, CustomPremiumForm, EditManagersForm, EditionForm, PledgeCancelForm
from regluit.frontend.forms import getTransferCreditForm, CCForm, CloneCampaignForm
from regluit.payment.manager import PaymentManager
from regluit.payment.models import Transaction, Account
from regluit.payment.parameters import TRANSACTION_STATUS_ACTIVE, TRANSACTION_STATUS_COMPLETE, TRANSACTION_STATUS_CANCELED, TRANSACTION_STATUS_ERROR, TRANSACTION_STATUS_FAILED, TRANSACTION_STATUS_INCOMPLETE, TRANSACTION_STATUS_NONE, TRANSACTION_STATUS_MODIFIED
from regluit.payment.parameters import PAYMENT_TYPE_AUTHORIZATION, PAYMENT_TYPE_INSTANT
from regluit.payment.parameters import PAYMENT_HOST_STRIPE
from regluit.payment.credit import credit_transaction
from regluit.core import goodreads
from tastypie.models import ApiKey
from regluit.payment.models import Transaction, Sent, CreditLog
from notification import models as notification

from regluit.payment import stripelib

logger = logging.getLogger(__name__)

def static_redirect_view(request, file_name, dir=""):
    return HttpResponseRedirect('/static/'+dir+"/"+file_name)

def slideshow(max):
    ending = models.Campaign.objects.filter(status='ACTIVE').order_by('deadline')
    count = ending.count()
    j = 0
        
    worklist = []
    if max > count:
        # add all the works with active campaigns
        for campaign in ending:
            worklist.append(campaign.work)

        # then fill out the rest of the list with popular but inactive works
        remainder = max - count
        remainder_works = models.Work.objects.exclude(campaigns__status='ACTIVE').order_by('-num_wishes')[:remainder]
        worklist.extend(remainder_works)
    else:
        # if the active campaign list has more works than we can fit 
        # in our slideshow, it's the only source we need to draw from
        while j < max:
            worklist.append(ending[j].work)
            j +=1
                
    return worklist

def next(request):
    if request.COOKIES.has_key('next'):
        response = HttpResponseRedirect(urllib.unquote(urllib.unquote(request.COOKIES['next'])))
        response.delete_cookie('next')
        return response
    else:
        return HttpResponseRedirect('/')
        
def safe_get_work(work_id):
    """
    use this rather than querying the db directly for a work by id
    """
    try:
        work = models.Work.objects.get(id = work_id)
    except models.Work.DoesNotExist:
        try:
            work = models.WasWork.objects.get(was = work_id).work
        except models.WasWork.DoesNotExist:
            raise Http404
    return work

def home(request, landing=False):
    if request.user.is_authenticated() and landing == False:
        return HttpResponseRedirect(reverse('supporter',
            args=[request.user.username]))

    worklist = slideshow(12)
    works = worklist[:6]
    works2 = worklist[6:12]

    events = models.Wishes.objects.order_by('-created')[0:2]
    return render(request, 'home.html', {'suppress_search_box': True, 'works': works, 'works2': works2, 'events': events})

def stub(request):
    path = request.path[6:] # get rid of /stub/
    return render(request,'stub.html', {'path': path})

def acks(request, work):
    return render(request,'front_matter.html', {'campaign': work.last_campaign()})
    
def work(request, work_id, action='display'):
    work = safe_get_work(work_id)
    if action == "acks":
        return acks( request, work)
        
    if request.method == 'POST' and not request.user.is_anonymous():
        activetab = '4'
    else:
        alert=''
        try:
            activetab = request.GET['tab']
            if activetab not in ['1', '2', '3', '4']:
                activetab = '1';
        except:
            activetab = '1';
            
    context = {}
    campaign = work.last_campaign()
    if campaign and campaign.edition and not request.user.is_staff:
        editions = [campaign.edition]
    else:
        editions = work.editions.all().order_by('-publication_date')
    try:
        pledged = campaign.transactions().filter(user=request.user, status="ACTIVE")
    except:
        pledged = None
        
    logger.info("pledged: {0}".format(pledged))
    countdown = ""
    
    try:
        assert not (work.last_campaign_status() == 'ACTIVE' and work.first_ebook())
    except:
        logger.warning("Campaign running for %s when ebooks are already available: why?" % work.title )
    
    if work.last_campaign_status() == 'ACTIVE':
        from math import ceil
        time_remaining = campaign.deadline - now()
        
        '''
        we want to round up on all of these; if it's the 3rd and the
        campaign ends the 8th, users expect to see 5 days remaining,
        not 4 (as an artifact of 4 days 11 hours or whatever)
        time_remaining.whatever is an int, so just adding 1 will do
        that for us (except in the case where .days exists and both other
        fields are 0, which is unlikely enough I'm not defending against it)
        '''
        if time_remaining.days:
            countdown = "in %s days" % str(time_remaining.days + 1)
        elif time_remaining.seconds > 3600:
            countdown = "in %s hours" % str(time_remaining.seconds/3600 + 1)
        elif time_remaining.seconds > 60:
            countdown = "in %s minutes" % str(time_remaining.seconds/60 + 1)
        else:
            countdown = "right now"
    
    if action == 'preview':
        work.last_campaign_status = 'ACTIVE'
        
    if not request.user.is_anonymous():
        claimform = UserClaimForm( request.user, data={'claim-work':work.pk, 'claim-user': request.user.id}, prefix = 'claim')
        for edition in editions:
            edition.hide_details = 1
            if request.method == 'POST' and not request.user.is_anonymous():
                if request.POST.has_key('ebook_%d-edition' % edition.id):
                    edition.ebook_form= EbookForm( data = request.POST, prefix = 'ebook_%d'%edition.id)
                    if edition.ebook_form.is_valid():
                        edition.ebook_form.save()
                        alert = 'Thanks for adding an ebook to unglue.it!'
                    else: 
                        edition.hide_details = 0
                        alert = 'your submitted ebook had errors'
            else:
                #edition.ebook_form = EbookForm( data = {'user':request.user.id, 'edition':edition.pk })
                edition.ebook_form = EbookForm( instance= models.Ebook(user = request.user, edition = edition, provider = 'x' ), prefix = 'ebook_%d'%edition.id)
    else:
        claimform = None
        
    if campaign:
        # pull up premiums explicitly tied to the campaign
        # mandatory premiums are only displayed in pledge process
        premiums = campaign.custom_premiums()
    else:
        premiums = None
        
    wishers = work.num_wishes
    base_url = request.build_absolute_uri("/")[:-1]
    
    active_claims = work.claim.all().filter(status='active')
    if active_claims.count() == 1:
        claimstatus = 'one_active'
        rights_holder_name = active_claims[0].rights_holder.rights_holder_name
    else:
        rights_holder_name = None
        pending_claims = work.claim.all().filter(status='pending')
        pending_claims_count = pending_claims.count()
        if pending_claims_count > 1:
          claimstatus = 'disputed'
        elif pending_claims_count == 1:
          claimstatus = 'one_pending'
          rights_holder_name = pending_claims[0].rights_holder.rights_holder_name
        else:
          claimstatus = 'open'
    
    return render(request, 'work.html', {
        'work': work, 
        'premiums': premiums, 
        'ungluers': userlists.supporting_users(work, 5), 
        'claimform': claimform,
        'wishers': wishers,
        'base_url': base_url,
        'editions': editions,
        'pledged': pledged,
        'activetab': activetab,
        'alert': alert,
        'claimstatus': claimstatus,
        'rights_holder_name': rights_holder_name,
        'countdown': countdown,
    })    

def new_edition(request, work_id, edition_id, by=None):
    if not request.user.is_authenticated() :
        return render(request, "admins_only.html")
    # if the work and edition are set, we save the edition and set the work    
    language='en'
    description=''
    title=''
    if work_id:
        try:
            work = models.Work.objects.get(id = work_id)
        except models.Work.DoesNotExist:
            try:
                work = models.WasWork.objects.get(was = work_id).work
            except models.WasWork.DoesNotExist:
                raise Http404
        language=work.language
        description=work.description
        title=work.title
    else:
        work=None
        
    if not request.user.is_staff :
        if by == 'rh' and work is not None:
            if not request.user in work.last_campaign().managers.all():
                return render(request, "admins_only.html")
        else:
            return render(request, "admins_only.html")
    if edition_id:
        try:
            edition = models.Edition.objects.get(id = edition_id)
        except models.Work.DoesNotExist:
            raise Http404
        if work:
            edition.work = work 
        language=edition.work.language
        description=edition.work.description
    else:
        edition = models.Edition()
        if work:
            edition.work = work 

    if request.method == 'POST' :
        edition.new_author_names=request.POST.getlist('new_author')
        edition.new_subjects=request.POST.getlist('new_subject')
        if request.POST.has_key('add_author_submit'):
            new_author_name = request.POST['add_author'].strip()
            try:
                author= models.Author.objects.get(name=new_author_name)
            except models.Author.DoesNotExist:
                author=models.Author.objects.create(name=new_author_name)
            edition.new_author_names.append(new_author_name)
            form = EditionForm(instance=edition, data=request.POST)
        elif request.POST.has_key('add_subject_submit'):
            new_subject = request.POST['add_subject'].strip()
            try:
                author= models.Subject.objects.get(name=new_subject)
            except models.Subject.DoesNotExist:
                author=models.Subject.objects.create(name=new_subject)
            edition.new_subjects.append(new_subject)
            form = EditionForm(instance=edition, data=request.POST)
        else:
            form = EditionForm(instance=edition, data=request.POST)
            if form.is_valid():
                form.save()
                if not work:
                    work= models.Work(title=form.cleaned_data['title'],language=form.cleaned_data['language'],description=form.cleaned_data['description'])
                    work.save()
                    edition.work=work
                    edition.save()
                else:
                    work.description=form.cleaned_data['description']
                    work.title=form.cleaned_data['title']
                    work.save()
                
                # note: this is very powerful. it can steal an isbn from another edition/work, and it will wipe the changed isbn from the db
                models.Identifier.set(type='isbn', value=form.cleaned_data['isbn_13'], edition=edition, work=work)
                
                if form.cleaned_data['oclcnum']:
                    # note: this is very powerful.(same comment as for isbn) use with care!
                    models.Identifier.set(type='oclc', value=form.cleaned_data['oclcnum'], edition=edition, work=work)
                for author_name in edition.new_author_names:
                    try:
                        author= models.Author.objects.get(name=author_name)
                    except models.Author.DoesNotExist:
                        author=models.Author.objects.create(name=author_name)
                    author.editions.add(edition)
                for subject_name in edition.new_subjects:
                    try:
                        subject= models.Subject.objects.get(name=subject_name)
                    except models.Subject.DoesNotExist:
                        subject=models.Subject.objects.create(name=subject_name)
                    subject.works.add(work)
                work_url = reverse('work', kwargs={'work_id': edition.work.id})
                return HttpResponseRedirect(work_url)
    else:
        form = EditionForm(instance=edition, initial={
            'language':language,
            'isbn_13':edition.isbn_13, 
            'oclcnum':edition.oclc,
            'description':description,
            'title': title
            })

    return render(request, 'new_edition.html', {
            'form': form, 'edition': edition
        })
    
    
def manage_campaign(request, id):
    campaign = get_object_or_404(models.Campaign, id=id)
    campaign.not_manager=False
    campaign.problems=[]
    if (not request.user.is_authenticated) or (not request.user in campaign.managers.all() and not request.user.is_staff):
        campaign.not_manager=True
        return render(request, 'manage_campaign.html', {'campaign': campaign})
    alerts = []
    activetab = '#1'

    if request.method == 'POST' :
        if request.POST.has_key('add_premium') :
            postcopy=request.POST.copy()
            postcopy['type']='CU'
            new_premium_form = CustomPremiumForm(data=postcopy)
            if new_premium_form.is_valid():
                new_premium_form.save()
                alerts.append(_('New premium has been added'))
                new_premium_form = CustomPremiumForm(data={'campaign': campaign})
            else:
                alerts.append(_('New premium has not been added'))              
            form = getManageCampaignForm(instance=campaign)
            activetab = '#2'
        elif request.POST.has_key('save') or request.POST.has_key('launch') :
            form= getManageCampaignForm(instance=campaign, data=request.POST)  
            if form.is_valid():     
                form.save() 
                campaign.update_left()
                alerts.append(_('Campaign data has been saved'))
                activetab = '#2'
            else:
                alerts.append(_('Campaign data has NOT been saved'))
            if 'launch' in request.POST.keys():
                activetab = '#3'
                if (campaign.launchable and form.is_valid()) and (not settings.IS_PREVIEW or request.user.is_staff):
                    campaign.activate()
                    alerts.append(_('Campaign has been launched'))
                else:
                    alerts.append(_('Campaign has NOT been launched'))
            new_premium_form = CustomPremiumForm(data={'campaign': campaign})
        elif request.POST.has_key('inactivate') :
            activetab = '#2'
            if request.POST.has_key('premium_id'):
                premiums_to_stop = request.POST.getlist('premium_id')
                for premium_to_stop in premiums_to_stop:
                    selected_premium = models.Premium.objects.get(id=premium_to_stop)
                    if selected_premium.type == 'CU':
                        selected_premium.type = 'XX'
                        selected_premium.save()
                        alerts.append(_('Premium %s has been inactivated'% premium_to_stop))   
            form = getManageCampaignForm(instance=campaign)
            new_premium_form = CustomPremiumForm(data={'campaign': campaign})
    else:
        form = getManageCampaignForm(instance=campaign)
        new_premium_form = CustomPremiumForm(data={'campaign': campaign})
        
    work = campaign.work
               
    return render(request, 'manage_campaign.html', {
        'campaign': campaign, 
        'form':form, 
        'problems': campaign.problems, 
        'alerts': alerts, 
        'premiums' : campaign.effective_premiums(),
        'premium_form' : new_premium_form,
        'work': work,
        'activetab': activetab,
        'is_preview': settings.IS_PREVIEW
    })
        
def googlebooks(request, googlebooks_id):
    try: 
        edition = models.Identifier.objects.get(type='goog',value=googlebooks_id).edition
    except models.Identifier.DoesNotExist:
        try:
            edition = bookloader.add_by_googlebooks_id(googlebooks_id)
            if edition.new:
                # add related editions asynchronously
                tasks.populate_edition.delay(edition.isbn_13)
        except bookloader.LookupFailure:
            logger.warning("failed to load googlebooks_id %s" % googlebooks_id)
            return HttpResponseNotFound("failed looking up googlebooks id %s" % googlebooks_id)
    if not edition:
        return HttpResponseNotFound("invalid googlebooks id")
    work_url = reverse('work', kwargs={'work_id': edition.work.id})
    return HttpResponseRedirect(work_url)

def subjects(request):
    order = request.GET.get('order')
    subjects = models.Subject.objects.all()
    subjects = subjects.annotate(Count('works'))

    if request.GET.get('order') == 'count':
        subjects = subjects.order_by('-works__count')
    else:
        subjects = subjects.order_by('name')

    return render(request, 'subjects.html', {'subjects': subjects})

class FilterableListView(ListView):
    def get_queryset(self):
        if self.request.GET.has_key('pub_lang'):
            if self.model is models.Campaign:
                return self.get_queryset_all().filter(work__language=self.request.GET['pub_lang'])
            else:
                return self.get_queryset_all().filter(language=self.request.GET['pub_lang'])
        else:
            return self.get_queryset_all()
            
    def get_context_data(self, **kwargs):
        context = super(FilterableListView, self).get_context_data(**kwargs)
        if self.request.GET.has_key('pub_lang'):
            context['pub_lang']=self.request.GET['pub_lang']
        else:
            context['pub_lang']=''
        context['show_langs']=True
        context['WISHED_LANGS']=settings.WISHED_LANGS
        return context

recommended_user = User.objects.filter( username=settings.UNGLUEIT_RECOMMENDED_USERNAME)

class WorkListView(FilterableListView):
    template_name = "work_list.html"
    context_object_name = "work_list"
    
    def get_queryset_all(self):
        facet = self.kwargs['facet']
        if (facet == 'popular'):
            return models.Work.objects.order_by('-num_wishes', 'id')
        elif (facet == 'recommended'):
            return models.Work.objects.filter(wishlists__user=recommended_user).order_by('-num_wishes')
        elif (facet == 'new'):
            return models.Work.objects.filter(num_wishes__gt=0).order_by('-created', '-num_wishes' ,'id')
        else:
            return models.Work.objects.all().order_by('-created', 'id')
            
    def get_context_data(self, **kwargs):
            context = super(WorkListView, self).get_context_data(**kwargs)
            qs=self.get_queryset()
            context['ungluers'] = userlists.work_list_users(qs,5)
            context['facet'] = self.kwargs['facet']
            context['works_unglued'] = qs.filter(editions__ebooks__isnull=False).distinct()[:20]
            context['works_active'] = qs.exclude(editions__ebooks__isnull=False).filter(Q(campaigns__status='ACTIVE') | Q(campaigns__status='SUCCESSFUL')).distinct()[:20]
            context['works_wished'] = qs.exclude(editions__ebooks__isnull=False).exclude(campaigns__status='ACTIVE').exclude(campaigns__status='SUCCESSFUL').distinct()[:20]
            
            context['activetab'] = "#3"
            
            counts={}
            counts['unglued'] = context['works_unglued'].count()
            counts['unglueing'] = context['works_active'].count()
            counts['wished'] = context['works_wished'].count()
            context['counts'] = counts
            
            return context

class UngluedListView(FilterableListView):
    template_name = "unglued_list.html"
    context_object_name = "work_list"
    
    def work_set_counts(self,work_set):
        counts={}
        counts['unglued'] = work_set.annotate(ebook_count=Count('editions__ebooks')).filter(ebook_count__gt=0).count()
        return counts

    def get_queryset_all(self):
        facet = self.kwargs['facet']
        if (facet == 'popular'):
            return models.Work.objects.filter(editions__ebooks__isnull=False).distinct().order_by('-num_wishes')
        else:
            #return models.Work.objects.annotate(ebook_count=Count('editions__ebooks')).filter(ebook_count__gt=0).order_by('-created')
            return models.Work.objects.filter(editions__ebooks__isnull=False).distinct().order_by('-created')

    def get_context_data(self, **kwargs):
            context = super(UngluedListView, self).get_context_data(**kwargs)
            qs=self.get_queryset()
            context['counts'] = self.work_set_counts(qs)
            context['ungluers'] = userlists.work_list_users(qs,5)
            context['facet'] =self.kwargs['facet']
            context['activetab'] = "#1"
            return context

        
class CampaignListView(FilterableListView):
    template_name = "campaign_list.html"
    context_object_name = "campaign_list"
    model = models.Campaign
    
    def get_queryset_all(self):
        facet = self.kwargs['facet']
        if (facet == 'newest'):
            return models.Campaign.objects.filter(status='ACTIVE').order_by('-activated')
        elif (facet == 'pledged'):
            return models.Campaign.objects.filter(status='ACTIVE').annotate(total_pledge=Sum('transaction__amount')).order_by('-total_pledge')
        elif (facet == 'pledges'):
            return models.Campaign.objects.filter(status='ACTIVE').annotate(pledges=Count('transaction')).order_by('-pledges')
        elif (facet == 'almost'):
            return models.Campaign.objects.filter(status='ACTIVE').all() # STUB: will need to make db changes to make this work 
        elif (facet == 'ending'):
            return models.Campaign.objects.filter(status='ACTIVE').order_by('deadline')
        else:
            return models.Campaign.objects.all()

    def get_context_data(self, **kwargs):
            context = super(CampaignListView, self).get_context_data(**kwargs)
            qs=self.get_queryset()
            context['ungluers'] = userlists.campaign_list_users(qs,5)
            context['facet'] =self.kwargs['facet']
            return context

class DonationView(TemplateView):
    template_name = "donation.html"
    
    def get(self, request, *args, **kwargs): 
        context = self.get_context_data()
        context['transfer_form']=getTransferCreditForm(self.request.user.credit.available)
        return self.render_to_response(context)
        
    def post(self, request, *args, **kwargs): 
        context = self.get_context_data()
        transfer_form=getTransferCreditForm(self.request.user.credit.available, data=self.request.POST)
        if transfer_form.is_valid():
            if self.request.user.credit.transfer_to(transfer_form.cleaned_data['recipient'], transfer_form.cleaned_data['amount']):
                #successful transfer
                context['transfer_message'] = 'Your transfer has been successfully executed.'
                context['recipient']= transfer_form.cleaned_data['recipient']
                context['transfer_amount'] = transfer_form.cleaned_data['amount']
                context['transfer_form']=getTransferCreditForm(self.request.user.credit.available)
            else:
                #unsuccessful transfer
                context['transfer_message'] = 'Your transfer was not successful.'
                context['transfer_form']=transfer_form
        else:
            #not valid
            context['transfer_form']=transfer_form
        return self.render_to_response(context)
        
    def get_context_data(self, *args, **kwargs):
        context = {'user' : self.request.user,'nonprofit': settings.NONPROFIT}
        context['donate_form'] = DonateForm(initial={'username':self.request.user.username})
        return context
            
class PledgeView(FormView):
    template_name="pledge.html"
    form_class = CampaignPledgeForm
    transaction = None
    campaign = None
    work = None
    premiums = None
    data = None
    
    def get_preapproval_amount(self):
        preapproval_amount = self.request.REQUEST.get('preapproval_amount', None)
        if preapproval_amount:
            return preapproval_amount
        premium_id = self.request.REQUEST.get('premium_id', None)
        if premium_id != None:
            try:
                preapproval_amount = D(models.Premium.objects.get(id=premium_id).amount)
            except:
                preapproval_amount = None
        if self.transaction:
            if preapproval_amount: 
                preapproval_amount = preapproval_amount if preapproval_amount>self.transaction.amount else self.transaction.amount
            else:
                preapproval_amount = self.transaction.amount
        return preapproval_amount
    
    def get_form_kwargs(self):
        assert self.request.user.is_authenticated()
        self.work = get_object_or_404(models.Work, id=self.kwargs["work_id"])
        
        # if there is no campaign or if campaign is not active, we should raise an error
        try:
            self.campaign = self.work.last_campaign()
             # TODO need to sort the premiums
            self.premiums = self.campaign.custom_premiums() | models.Premium.objects.filter(id=150)
            # Campaign must be ACTIVE
            assert self.campaign.status == 'ACTIVE'           
        except Exception, e:
            raise e

        transactions = self.campaign.transactions().filter(user=self.request.user, status=TRANSACTION_STATUS_ACTIVE, type=PAYMENT_TYPE_AUTHORIZATION)
        premium_id = self.request.REQUEST.get('premium_id', None)
        if transactions.count() == 0:
            ack_name=''
            ack_dedication=''
            anonymous=''
        else:
            self.transaction = transactions[0]   
            if premium_id == None and self.transaction.premium is not None:
                premium_id = self.transaction.premium.id
            ack_name=self.transaction.ack_name
            ack_dedication=self.transaction.ack_dedication
            anonymous=self.transaction.anonymous

        self.data = {'preapproval_amount':self.get_preapproval_amount(), 'premium_id':premium_id, 
                    'ack_name':ack_name, 'ack_dedication':ack_dedication, 'anonymous':anonymous}
        if self.request.method  == 'POST':
            self.data.update(self.request.POST.dict())
            return {'data':self.data}
        else:
            return {'initial':self.data}
        
    def get_context_data(self, **kwargs):
        """set up the pledge page"""
        
        context = super(PledgeView, self).get_context_data(**kwargs)
              
        context.update({
                'work':self.work,
                'campaign':self.campaign, 
                'premiums':self.premiums, 
                'premium_id':self.data['premium_id'], 
                'faqmenu': 'modify' if self.transaction else 'pledge', 
                'transaction': self.transaction,
                'tid': self.transaction.id if self.transaction else None,
           })
            
        return context
    
    def form_valid(self, form):
        # right now, if there is a non-zero pledge amount, go with that. otherwise, do the pre_approval
        
        p = PaymentManager()
        if self.transaction:
            # modifying the transaction...
            assert self.transaction.type == PAYMENT_TYPE_AUTHORIZATION and self.transaction.status == TRANSACTION_STATUS_ACTIVE        
            status,  url = p.modify_transaction(self.transaction, form.cleaned_data["preapproval_amount"],  
                    paymentReason="Unglue.it Pledge for {0}".format(self.campaign.name), 
                    pledge_extra=form.pledge_extra
                    )
            logger.info("status: {0}, url:{1}".format(status, url))
            
            if status and url is not None:
                logger.info("PledgeView (Modify): " + url)
                return HttpResponseRedirect(url)
            elif status and url is None:
                return HttpResponseRedirect("{0}?tid={1}".format(reverse('pledge_modified'), self.transaction.id))
            else:
                return HttpResponse("No modification made")
        else:
            t, url = p.process_transaction('USD',  form.cleaned_data["preapproval_amount"],  
                    host = None, 
                    campaign=self.campaign, 
                    user=self.request.user,
                    paymentReason="Unglue.it Pledge for {0}".format(self.campaign.name), 
                    pledge_extra=form.pledge_extra
                    )    
            if url:
                logger.info("PledgeView url: " + url)
                return HttpResponseRedirect(url)
            else:
                logger.error("Attempt to produce transaction id {0} failed".format(t.id))
                return HttpResponse("Our attempt to enable your transaction failed. We have logged this error.")

class FundPledgeView(FormView):
    template_name="fund_the_pledge.html"
    form_class = CCForm
    transaction = None

    def get_form_kwargs(self):
        kwargs = super(FundPledgeView, self).get_form_kwargs()
        
        assert self.request.user.is_authenticated()
        if self.transaction is None:
            self.transaction = get_object_or_404(Transaction, id=self.kwargs["t_id"])
        
        if kwargs.has_key('data'):
            data = kwargs['data'].copy()
        else:
            data = {}
        
        data.update(
            {'preapproval_amount':self.transaction.max_amount,
                'username':self.request.user.username,
                'work_id':self.transaction.campaign.work.id,
                'title':self.transaction.campaign.work.title}
            )
        
        kwargs['data'] = data
        
        return kwargs

    def get_context_data(self, **kwargs):
        context = super(FundPledgeView, self).get_context_data(**kwargs)
        context['modified'] = self.transaction.status==TRANSACTION_STATUS_MODIFIED
        context['preapproval_amount']=self.transaction.max_amount
        context['needed'] = self.transaction.max_amount - self.request.user.credit.available
        context['transaction']=self.transaction
        context['nonprofit'] = settings.NONPROFIT
        context['STRIPE_PK'] = stripelib.STRIPE_PK
        # note that get_form_kwargs() will already have been called once
        donate_args=self.get_form_kwargs()
        donate_args['data']['preapproval_amount']=context['needed']
        context['donate_form'] = DonateForm(**donate_args)
        return context
    
    def post(self, request, *args, **kwargs):
        logger.info('request.POST: {0}'.format(request.POST))
        return super(FundPledgeView, self).post(request, *args, **kwargs)
    
    def form_valid(self, form):
        """ note desire to pledge; make sure there is a credit card to charge"""
        
        # first pass -- we have a token  -- also do more direct coupling to stripelib -- then move to
        # abstraction of payment.manager / payment.baseprocessor
        
        # demonstrate two possibilities:  1) token -> charge or 2) token->customer->charge
        
        stripe_token = form.cleaned_data["stripe_token"]
        preapproval_amount = form.cleaned_data["preapproval_amount"]
        retain_cc_info = form.cleaned_data["retain_cc_info"]

        sc = stripelib.StripeClient()
        
        # let's figure out what part of transaction can be used to store info
        # try placing charge id in transaction.pay_key
        # need to set amount
        # how does max_amount get set? -- coming from /pledge/xxx/?
        # max_amount is set -- but I don't think we need it for stripe
        
        if retain_cc_info:
            # create customer and charge id and then charge the customer
            customer = sc.create_customer(card=stripe_token, description=self.request.user.username,
                                          email=self.request.user.email)
                
            account = Account(host = PAYMENT_HOST_STRIPE,
                              account_id = customer.id,
                              card_last4 = customer.active_card.last4,
                              card_type = customer.active_card.type,
                              card_exp_month = customer.active_card.exp_month,
                              card_exp_year = customer.active_card.exp_year,
                              card_fingerprint = customer.active_card.fingerprint,
                              card_country = customer.active_card.country,
                              user = self.request.user
                              )

            account.save()
            
            charge = sc.create_charge(preapproval_amount, customer=customer, description="${0} for test / retain cc".format(preapproval_amount))
 
        else:
            customer = None
            
            charge = sc.create_charge(preapproval_amount, card=stripe_token, description="${0} for test / cc not retained".format(preapproval_amount))
        
        # set True for now -- wondering whether we should actually wait for a webhook -- don't think so.
        
        ## settings to apply to transaction for TRANSACTION_STATUS_COMPLETE
        #self.transaction.type = PAYMENT_TYPE_INSTANT
        #self.transaction.approved = True
        #self.transaction.status = TRANSACTION_STATUS_COMPLETE
        #self.transaction.pay_key = charge.id
        
        # settings to apply to transaction for TRANSACTION_STATUS_ACTIVE
        # should approved be set to False and wait for a webhook?
        self.transaction.type = PAYMENT_TYPE_AUTHORIZATION
        self.transaction.approved = True
        self.transaction.status = TRANSACTION_STATUS_ACTIVE
        self.transaction.preapproval_key = charge.id        
        
        self.transaction.currency = 'USD'
        self.transaction.amount = preapproval_amount
        self.transaction.date_payment = now()
            
        self.transaction.save()
            
        return HttpResponse("charge id: {0} / customer: {1}".format(charge.id, customer))

        
class NonprofitCampaign(FormView):
    template_name="nonprofit.html"
    form_class = CCForm
    
    def get_context_data(self, **kwargs):
        context = super(NonprofitCampaign, self).get_context_data(**kwargs)
        context['nonprofit'] = settings.NONPROFIT
        context['get'] = self.request.GET
        return context

    def get_form_kwargs(self):
        if self.request.method == 'POST':
            return {'data':self.request.POST}
        else:
            return {'initial':self.request.GET }

        

    def form_valid(self, form):
        username=form.cleaned_data['username']
        forward={'username':username}
        forward['work_id']= form.cleaned_data['work_id']
        amount=form.cleaned_data['preapproval_amount']
        forward['cents']=int(100*(amount-int(amount)))
        forward['amount']= int(amount)
        forward['sent']= Sent.objects.create(user=username,amount=form.cleaned_data['preapproval_amount']).pk
        token=signing.dumps(forward)
        return HttpResponseRedirect(settings.BASE_URL + reverse('donation_credit',kwargs={'token':token}))

class DonationCredit(TemplateView):
    template_name="donation_credit.html"

    def get_context_data(self, **kwargs):
        context = super(DonationCredit, self).get_context_data(**kwargs)
        context['faqmenu']="donation"
        context['nonprofit'] = settings.NONPROFIT
        try:
            envelope=signing.loads(kwargs['token'])
            context['envelope']=envelope
        except signing.BadSignature:
            self.template_name="donation_error.html"
            return context
        try:
            work = models.Work.objects.get(id=envelope['work_id'])
            campaign=work.last_campaign()
        except models.Work.DoesNotExist:
            campaign = None
        context['work']=work
        try:
            user = User.objects.get(username=envelope['username'])
        except User.DoesNotExist:
            self.template_name="donation_user_error.html"
            context['error']='user does not exist'
            return context
        if user != self.request.user:
            self.template_name="donation_user_error.html"
            context['error']='wrong user logged in'
            return context
        try:
            # check token not used
            CreditLog.objects.get(sent=envelope['sent'])
            context['error']='credit already registered'
            return context
        except CreditLog.DoesNotExist:
            #not used yet!
            amount=envelope['amount']+envelope['cents']/D(100)
            CreditLog.objects.create(user=user,amount=amount,action='deposit',sent=envelope['sent'])
            ts=Transaction.objects.filter(user=user,campaign=campaign,status=TRANSACTION_STATUS_NONE).order_by('-pk')
            if ts.count()==0:
                ts=Transaction.objects.filter(user=user,campaign=campaign,status=TRANSACTION_STATUS_MODIFIED).order_by('-pk')
            if ts.count()>0:
                t=ts[0]
                credit_transaction(t,user, amount)
                for t in ts[1:]:
                    t.status=TRANSACTION_STATUS_CANCELED
                    t.save()
                context['transaction']=t
                return context
            else:
                user.credit.add_to_balance(amount)
                return context
                
            
class PledgeRechargeView(TemplateView):
    """
    a view to allow for recharge of a transaction for failed transactions or ones with errors
    """
    template_name="pledge_recharge.html"

    def get_context_data(self, **kwargs):
        
        context = super(PledgeRechargeView, self).get_context_data(**kwargs)
        
        # the following should be true since PledgeView.as_view is wrapped in login_required
        assert self.request.user.is_authenticated()
        user = self.request.user
        
        work = get_object_or_404(models.Work, id=self.kwargs["work_id"])
        campaign = work.last_campaign()
        
        if campaign is None:
            return Http404
        
        transaction = campaign.transaction_to_recharge(user)
        
        # calculate a URL to do a preapproval -- in the future, we may want to do a straight up payment
            
        return_url = None
        nevermind_url = None
        
        if transaction is not None:
            # the recipients of this authorization is not specified here but rather by the PaymentManager.    
            paymentReason = "Unglue.it Recharge for {0}".format(campaign.name)
            
            p = PaymentManager()
            t, url = p.authorize(transaction, return_url=return_url, paymentReason=paymentReason)
            logger.info("Recharge url: {0}".format(url))
        else:
            url = None
        
        context.update({
                'work':work,
                'transaction':transaction,
                'payment_processor':transaction.host if transaction is not None else None,
                'recharge_url': url
                })
        return context
    

class PledgeCompleteView(TemplateView):
    """A callback for PayPal to tell unglue.it that a payment transaction has completed successfully.
    
    Possible things to implement:
    
        after pledging, supporter receives email including thanks, work pledged, amount, expiry date, any next steps they should expect; others?
    study other confirmation emails for their contents
    should note that a confirmation email has been sent to $email from $sender
    should briefly note next steps (e.g. if this campaign succeeds you will be emailed on date X)    
        
    """
    
    template_name="pledge_complete.html"
    
    def get_context_data(self):
        # pick up all get and post parameters and display
        context = super(PledgeCompleteView, self).get_context_data()
        
        if self.request.user.is_authenticated():
            user = self.request.user
        else:
            user = None
        
        # pull out the transaction id and try to get the corresponding Transaction
        transaction_id = self.request.REQUEST.get("tid")
        transaction = Transaction.objects.get(id=transaction_id)
        
        # work and campaign in question
        try:
            campaign = transaction.campaign
            work = campaign.work
        except Exception, e:
            campaign = None
            work = None
        
        # we need to check whether the user tied to the transaction is indeed the authenticated user.
        try:
            if user.id != transaction.user.id:
                # should be 403 -- but let's try 404 for now -- 403 exception coming in Django 1.4
                raise Http404
        except Exception, e:
            raise Http404
            
            
        # check that the user had not already approved the transaction
        # do we need to first run PreapprovalDetails to check on the status
        
        # is it of type=PAYMENT_TYPE_AUTHORIZATION and status is NONE or ACTIVE (but approved is false)
        
        if transaction.type == PAYMENT_TYPE_AUTHORIZATION:
            correct_transaction_type = True
        else:
            correct_transaction_type = False
            
        # add the work corresponding to the Transaction on the user's wishlist if it's not already on the wishlist
        if user is not None and correct_transaction_type and (campaign is not None) and (work is not None):
            # ok to overwrite Wishes.source?
            user.wishlist.add_work(work, 'pledging')
            
        worklist = slideshow(8)
        works = worklist[:4]
        works2 = worklist[4:8]

        context["transaction"] = transaction
        context["work"] = work
        context["campaign"] = campaign
        context["faqmenu"] = "complete"
        context["works"] = works
        context["works2"] = works2   
        context["site"] = Site.objects.get_current()
        
        return context        

class PledgeModifiedView(PledgeCompleteView):
    def get_context_data(self):
        context = super(PledgeModifiedView, self).get_context_data()
        context['modified']=True
        return context
    
class PledgeCancelView(FormView):
    """A view for allowing a user to cancel the active transaction for specified campaign"""
    template_name="pledge_cancel.html"
    form_class = PledgeCancelForm
    
    def get_context_data(self, **kwargs):
        context = super(PledgeCancelView, self).get_context_data(**kwargs)
        
        # initialize error to be None
        context["error"] = None
        
        # the following should be true since PledgeCancelView.as_view is wrapped in login_required
        
        if self.request.user.is_authenticated():
            user = self.request.user
        else:
            context["error"] = "You are not logged in."
            return context
        
        campaign = get_object_or_404(models.Campaign, id=self.kwargs["campaign_id"])
        if campaign.status != 'ACTIVE':
            context["error"] = "{0} is not an active campaign".format(campaign)
            return context
        
        work = campaign.work
        transactions = campaign.transactions().filter(user=user, status=TRANSACTION_STATUS_ACTIVE)
        
        if transactions.count() < 1:
            context["error"] = "You don't have an active transaction for this campaign."
            return context
        elif transactions.count() > 1:
            logger.error("User {0} has {1} active transactions for campaign id {2}".format(user, transactions.count(), campaign.id))
            context["error"] = "You have {0} active transactions for this campaign".format(transactions.count())
            return context

        transaction = transactions[0]
        if transaction.type != PAYMENT_TYPE_AUTHORIZATION:
            logger.error("Transaction id {0} transaction type, which should be {1}, is actually {2}".format(transaction.id, PAYMENT_TYPE_AUTHORIZATION, transaction.type))
            context["error"] = "Your transaction type, which should be {0}, is actually {1}".format(PAYMENT_TYPE_AUTHORIZATION, transaction.type)
            return context
        
        # we've located the transaction, work, and campaign referenced in the view
        
        context["transaction"] = transaction
        context["work"] = work
        context["campaign"] = campaign
        context["faqmenu"] = "cancel"
        
        return context
    
    def form_valid(self, form):
        # check that user does, in fact, have an active transaction for specified campaign
    
        logger.info("arrived at pledge_cancel form_valid")
        # pull campaign_id from form, not from URI as we do from GET
        campaign_id = self.request.REQUEST.get('campaign_id')
        
        # this following logic should be extraneous.
        if self.request.user.is_authenticated():
            user = self.request.user
        else:
            return HttpResponse("You need to be logged in.")
        
        try:
            # look up the specified campaign and attempt to pull up the appropriate transaction
            # i.e., the transaction actually belongs to user, that the transaction is active
            campaign = get_object_or_404(models.Campaign, id=self.kwargs["campaign_id"], status='ACTIVE')
            transaction = campaign.transaction_set.get(user=user, status=TRANSACTION_STATUS_ACTIVE,
                                                          type=PAYMENT_TYPE_AUTHORIZATION)
            # attempt to cancel the transaction and redirect to the Work page if cancel is successful
            # here's a place that would be nice to use https://docs.djangoproject.com/en/dev/ref/contrib/messages/
            # to display the success or failure of the cancel operation as a popup in the context of the work page
            p = PaymentManager()
            result = p.cancel_transaction(transaction)
            # put a notification here for pledge cancellation?
            if result:
                # Now if we redirect the user to the Work page and the IPN hasn't arrived, the status of the
                # transaction might be out of date.  Let's try an explicit polling of the transaction result before redirecting
                # We might want to remove this in a production system
                if settings.DEBUG:
                    update_status = p.update_preapproval(transaction)
                # send a notice out that the transaction has been canceled -- leverage the pledge_modify notice for now
                # BUGBUG:  should have a pledge cancel notice actually since I think it's different
                from regluit.payment.signals import pledge_modified
                pledge_modified.send(sender=self, transaction=transaction, up_or_down="canceled")
                logger.info("pledge_modified notice for cancellation: sender {0}, transaction {1}".format(self, transaction))
                return HttpResponseRedirect(reverse('work', kwargs={'work_id': campaign.work.id}))
            else:
                logger.error("Attempt to cancel transaction id {0} failed".format(transaction.id))
                return HttpResponse("Our attempt to cancel your transaction failed. We have logged this error.")
        except Exception, e:
            logger.error("Exception from attempt to cancel pledge for campaign id {0} for username {1}: {2}".format(campaign_id, user.username, e))
            return HttpResponse("Sorry, something went wrong in canceling your campaign pledge. We have logged this error.")    
    
def claim(request):
    if  request.method == 'GET': 
        data = request.GET
    else:
        data =  request.POST
    form =  UserClaimForm(request.user, data=data, prefix='claim')
    if form.is_valid():
        # make sure we're not creating a duplicate claim
        if not models.Claim.objects.filter(work=data['claim-work'], rights_holder=data['claim-rights_holder'], status='pending').count():
            form.save()
        return HttpResponseRedirect(reverse('work', kwargs={'work_id': data['claim-work']}))
    else:
        work = models.Work.objects.get(id=data['claim-work'])
        rights_holder = models.RightsHolder.objects.get(id=data['claim-rights_holder'])
        active_claims = work.claim.exclude(status = 'release')
        context = {'form': form, 'work': work, 'rights_holder':rights_holder , 'active_claims':active_claims}
        return render(request, "claim.html", context)

def rh_tools(request):
    if not request.user.is_authenticated() :
        return render(request, "rh_tools.html")
    claims = request.user.claim.filter(user=request.user)
    campaign_form = "xxx"
    if not claims:
        return render(request, "rh_tools.html")
    for claim in claims:
        if claim.status == 'active':
            claim.campaigns = claim.work.campaigns.all()
        else:
            claim.campaigns = []
        claim.can_open_new=False if claim.work.last_campaign_status in ['ACTIVE','INITIALIZED'] else True
        for campaign in claim.campaigns:
            if campaign.status in ['ACTIVE','INITIALIZED']:
                claim.can_open_new=False
                if request.method == 'POST' and request.POST.has_key('edit_managers_%s'% campaign.id) :
                    campaign.edit_managers_form=EditManagersForm( instance=campaign, data=request.POST, prefix=campaign.id)
                    if campaign.edit_managers_form.is_valid():
                        campaign.edit_managers_form.save()
                        campaign.edit_managers_form = EditManagersForm(instance=campaign, prefix=campaign.id)
                else:
                    campaign.edit_managers_form=EditManagersForm(instance=campaign, prefix=campaign.id)
        if claim.status == 'active' and claim.can_open_new:
            if request.method == 'POST' and  request.POST.has_key('work') and int(request.POST['work']) == claim.work.id :
                claim.campaign_form = OpenCampaignForm(request.POST)
                if claim.campaign_form.is_valid():                    
                    new_campaign = claim.campaign_form.save(commit=False)
                    new_campaign.deadline = date_today() + timedelta(days=int(settings.UNGLUEIT_LONGEST_DEADLINE))
                    new_campaign.target = D(settings.UNGLUEIT_MINIMUM_TARGET)
                    new_campaign.save()
                    claim.campaign_form.save_m2m()
                    claim.can_open_new=False
            else:
                claim.campaign_form = OpenCampaignForm(data={'work': claim.work, 'name': claim.work.title,  'userid': request.user.id, 'managers_1': request.user.id})
        else:
            claim.can_open_new=False
    campaigns = request.user.campaigns.all()
    new_campaign = None
    for campaign in campaigns:
        if campaign.clonable():
            if request.method == 'POST' and  request.POST.has_key('c%s-campaign_id'% campaign.id):
                clone_form= CloneCampaignForm(data=request.POST, prefix = 'c%s' % campaign.id)
                if clone_form.is_valid():
                    campaign.clone()
            else:
                campaign.clone_form= CloneCampaignForm(initial={'campaign_id':campaign.id}, prefix = 'c%s' % campaign.id)
    return render(request, "rh_tools.html", {'claims': claims ,'campaigns': campaigns}) 

def rh_admin(request):
    if not request.user.is_authenticated() :
        return render(request, "admins_only.html")
    if not request.user.is_staff :
        return render(request, "admins_only.html")
    PendingFormSet = modelformset_factory(models.Claim, fields=['status'], extra=0)
    pending_data = models.Claim.objects.filter(status = 'pending')
    active_data = models.Claim.objects.filter(status = 'active')
    if  request.method == 'POST': 
        if 'create_rights_holder' in request.POST.keys():
            form = RightsHolderForm(data=request.POST)
            pending_formset = PendingFormSet (queryset=pending_data)
            if form.is_valid():
                form.save()
                form = RightsHolderForm()
        if 'set_claim_status' in request.POST.keys():
            pending_formset = PendingFormSet (request.POST, request.FILES, queryset=pending_data)
            form = RightsHolderForm()
            if pending_formset.is_valid():
                pending_formset.save()
                pending_formset = PendingFormSet(queryset=pending_data)
    else:
        form = RightsHolderForm()
        pending_formset = PendingFormSet(queryset=pending_data)
    rights_holders = models.RightsHolder.objects.all()
    
    context = { 
        'request': request, 
        'rights_holders': rights_holders, 
        'form': form,
        'pending': zip(pending_data,pending_formset),
        'pending_formset': pending_formset,
        'active_data': active_data,
    }
    return render(request, "rights_holders.html", context)

def campaign_admin(request):
    if not request.user.is_authenticated() :
        return render(request, "admins_only.html")    
    if not request.user.is_staff :
        return render(request, "admins_only.html")
        
    context = {}
    
    def campaigns_types():
        # pull out Campaigns with Transactions that are ACTIVE -- and hence can be executed
        # Campaign.objects.filter(transaction__status='ACTIVE')
        
        campaigns_with_active_transactions = models.Campaign.objects.filter(transaction__status=TRANSACTION_STATUS_ACTIVE)
            
        # pull out Campaigns with Transactions that are INCOMPLETE
    
        campaigns_with_incomplete_transactions = models.Campaign.objects.filter(transaction__status=TRANSACTION_STATUS_INCOMPLETE)
        
        # show all Campaigns with Transactions that are COMPLETED
    
        campaigns_with_completed_transactions = models.Campaign.objects.filter(transaction__status=TRANSACTION_STATUS_COMPLETE)
        
        # show Campaigns with Transactions that are CANCELED
        
        campaigns_with_canceled_transactions = models.Campaign.objects.filter(transaction__status=TRANSACTION_STATUS_CANCELED)
        
        return (campaigns_with_active_transactions, campaigns_with_incomplete_transactions, campaigns_with_completed_transactions, campaigns_with_canceled_transactions)
        
    form = CampaignAdminForm()
    pm = PaymentManager()
    check_status_results = None
    command_status = None
    
    if request.method == 'GET':
        pass
    elif request.method == 'POST':
        if 'campaign_checkstatus' in request.POST.keys():
            # campaign_checkstatus
            try:
                status = pm.checkStatus()
                check_status_results = ""
                # parse the output to display chat transaction statuses have been updated
                if len(status["preapprovals"]):
                    for t in status["preapprovals"]:
                        check_status_results += "<p>Preapproval key: %s updated</p>" % (t["key"])
                else:
                    check_status_results += "<p>No preapprovals needed updating</p>"
                if len(status["payments"]):
                    for t in status["payments"]:
                        info = ", ".join(["%s:%s" % (k,v) for (k,v) in t.items()])
                        check_status_results += "<p>Payment updated: %s </p>" % (info)
                        
                else:
                    check_status_results += "<p>No payments needed updating</p>"                    
                command_status = _("Transactions updated based on PaymentDetails and PreapprovalDetails")
            except Exception, e:
                check_status_results = e
        elif 'execute_campaigns' in request.POST.keys():            
            c_id = request.POST.get('active_campaign', None)
            if c_id is not None:
                try:
                    campaign = models.Campaign.objects.get(id=c_id)
                    results = pm.execute_campaign(campaign)
                    command_status = str(results)
                except Exception, e:
                    command_status = "Error in executing transactions for campaign %s " % (str(e))
        elif 'finish_campaigns' in request.POST.keys():
            c_id = request.POST.get('incomplete_campaign', None)
            if c_id is not None:
                try:
                    campaign = models.Campaign.objects.get(id=c_id)
                    results = pm.finish_campaign(campaign)
                    command_status = str(results)
                except Exception, e:
                    command_status = "Error in finishing transactions for campaign %s " % (str(e))            
            
        elif 'cancel_campaigns' in request.POST.keys():
            c_id = request.POST.get('active_campaign', None)
            if c_id is not None:
                try:
                    campaign = models.Campaign.objects.get(id=c_id)
                    results = pm.cancel_campaign(campaign)
                    command_status = str(results)
                except Exception, e:
                    command_status = "Error in canceling transactions for campaign %s " % (str(e))        
            
    (campaigns_with_active_transactions, campaigns_with_incomplete_transactions, campaigns_with_completed_transactions,
                campaigns_with_canceled_transactions) = campaigns_types()
    
    context.update({
        'form': form,
        'check_status_results':check_status_results,
        'campaigns_with_active_transactions': campaigns_with_active_transactions,
        'campaigns_with_incomplete_transactions': campaigns_with_incomplete_transactions,
        'campaigns_with_completed_transactions': campaigns_with_completed_transactions,
        'campaigns_with_canceled_transactions': campaigns_with_canceled_transactions,
        'command_status': command_status
    })

    return render(request, "campaign_admin.html", context)

def supporter(request, supporter_username, template_name):
    supporter = get_object_or_404(User, username=supporter_username)
    wishlist = supporter.wishlist
    works = []
    works2 = []
    works_unglued = []
    works_active = []
    works_wished = []
    
    if (wishlist.works.all()):
        # querysets for tabs
        # unglued tab is anything with an existing ebook
        ## .order_by() may clash with .distinct() and this should be fixed
        works_unglued = wishlist.works.all().filter(editions__ebooks__isnull=False).distinct().order_by('-num_wishes')
        
        # take the set complement of the unglued tab and filter it for active works to get middle tab
        result = wishlist.works.all().exclude(pk__in=works_unglued.values_list('pk', flat=True))
        works_active = result.filter(Q(campaigns__status='ACTIVE') | Q(campaigns__status='SUCCESSFUL')).order_by('-campaigns__status', 'campaigns__deadline').distinct()
        
        # everything else goes in tab 3
        works_wished = result.exclude(pk__in=works_active.values_list('pk', flat=True)).order_by('-num_wishes')
        
        # badge counts
        backed = works_unglued.count()
        backing = works_active.count()
        wished = works_wished.count()
    
    else:           
        backed = 0
        backing = 0
        wished = 0
        
        worklist = slideshow(8)
        works = worklist[:4]
        works2 = worklist[4:8]
        
    # default to showing the Active tab if there are active campaigns, else show Wishlist
    if backing > 0:
        activetab = "#2"
    else:
        activetab = "#3"
    
    date = supporter.date_joined.strftime("%B %d, %Y")
    
    # following block to support profile admin form in supporter page
    if request.user.is_authenticated() and request.user.username == supporter_username:

        try:
            profile_obj=request.user.get_profile()
        except ObjectDoesNotExist:
            profile_obj= models.UserProfile()
            profile_obj.user=request.user

        if  request.method == 'POST': 
            profile_form = ProfileForm(data=request.POST,instance=profile_obj)
            if profile_form.is_valid():
                if profile_form.cleaned_data['clear_facebook'] or profile_form.cleaned_data['clear_twitter'] or  profile_form.cleaned_data['clear_goodreads'] :
                    if profile_form.cleaned_data['clear_facebook']:
                        profile_obj.facebook_id=0
                    if profile_form.cleaned_data['clear_twitter']:
                        profile_obj.twitter_id=""
                    if profile_form.cleaned_data['clear_goodreads']:
                        profile_obj.goodreads_user_id = None
                        profile_obj.goodreads_user_name = None
                        profile_obj.goodreads_user_link = None
                        profile_obj.goodreads_auth_token = None
                        profile_obj.goodreads_auth_secret = None

                    profile_obj.save()
                profile_form.save()

        else:
            profile_form= ProfileForm(instance=profile_obj)
        
        if request.user.profile.goodreads_user_id is not None:
            goodreads_id = request.user.profile.goodreads_user_id
        else:
            goodreads_id = None

        if request.user.profile.librarything_id is not None:
            librarything_id = request.user.profile.librarything_id
        else:
            librarything_id = None
    else:
        profile_form = ''
        goodreads_id = None
        librarything_id = None

    context = {
            "supporter": supporter,
            "wishlist": wishlist,
            "works_unglued": works_unglued,
            "works_active": works_active,
            "works_wished": works_wished,
            "works": works,
            "works2": works2,
            "backed": backed,
            "backing": backing,
            "wished": wished,
            "date": date,
            "profile_form": profile_form,
            "ungluers": userlists.other_users(supporter, 5 ),
            "goodreads_auth_url": reverse('goodreads_auth'),
            "goodreads_id": goodreads_id,
            "librarything_id": librarything_id,
            "activetab": activetab
    }
    
    return render(request, template_name, context)

def edit_user(request):
    if not request.user.is_authenticated():
        return HttpResponseRedirect(reverse('auth_login'))    
    form=UserData()
    emailform = UserEmail()
    if request.method == 'POST': 
        if 'change_username' in request.POST.keys():
            form = UserData(request.POST)
            form.oldusername = request.user.username
            if form.is_valid(): # All validation rules pass, go and change the username
                request.user.username=form.cleaned_data['username']
                request.user.save()
                return HttpResponseRedirect(reverse('home')) # Redirect after POST
        elif 'change_email'  in request.POST.keys():
            emailform = UserEmail(request.POST)
            emailform.oldemail = request.user.email
            if emailform.is_valid():
                request.user.email=emailform.cleaned_data['email']
                request.user.save()
                send_mail_task.delay(
                    'unglue.it email changed', 
                    render_to_string('registration/email_changed.txt',{'oldemail':emailform.oldemail,'request':request}),
                    None,
                    [request.user.email,emailform.oldemail]
                    )
                return HttpResponseRedirect(reverse('home')) # Redirect after POST
    return render(request,'registration/user_change_form.html', {'form': form,'emailform': emailform})  


def search(request):
    q = request.GET.get('q', None)
    page = int(request.GET.get('page', 1))
    results = gluejar_search(q, user_ip=request.META['REMOTE_ADDR'], page=page)

    # flag search result as on wishlist as appropriate
    if not request.user.is_anonymous():
        ungluers = userlists.other_users(request.user, 5)
    else:
        ungluers = userlists.other_users(None, 5)

    works=[]
    for result in results:
        try:
            work = models.Identifier.objects.get(type='goog',value=result['googlebooks_id']).work
            works.append(work)
        except models.Identifier.DoesNotExist: 
            works.append(result)
    context = {
        "q": q,
        "results": works,
        "ungluers": ungluers
    }
    return render(request, 'search.html', context)

# TODO: perhaps this functionality belongs in the API?
@require_POST
@login_required
@csrf_exempt
def wishlist(request):
    googlebooks_id = request.POST.get('googlebooks_id', None)
    remove_work_id = request.POST.get('remove_work_id', None)
    add_work_id = request.POST.get('add_work_id', None)

    if googlebooks_id:
        try:
            edition = bookloader.add_by_googlebooks_id(googlebooks_id)
            if edition.new:
                # add related editions asynchronously
                tasks.populate_edition.delay(edition.isbn_13)
            request.user.wishlist.add_work(edition.work,'user')
        except bookloader.LookupFailure:
            logger.warning("failed to load googlebooks_id %s" % googlebooks_id)
        except Exception, e:
            logger.warning("Error in wishlist adding %s" % (e))          
        # TODO: redirect to work page, when it exists
        return HttpResponseRedirect('/')
    elif remove_work_id:
        try:
            work = models.Work.objects.get(id=int(remove_work_id))
        except models.Work.DoesNotExist:
            try:
                work = models.WasWork.objects.get(was = work_id).work
            except models.WasWork.DoesNotExist:
                raise Http404
        request.user.wishlist.remove_work(work)
        # TODO: where to redirect?
        return HttpResponseRedirect('/')
    elif add_work_id:
        # if adding from work page, we have may work.id, not googlebooks_id
        try:
            work = models.Work.objects.get(pk=add_work_id)
        except models.Work.DoesNotExist:
            try:
                work = models.WasWork.objects.get(was = work_id).work
            except models.WasWork.DoesNotExist:
                raise Http404

        request.user.wishlist.add_work(work,'user')
        return HttpResponseRedirect('/')
  
class InfoPageView(TemplateView):
    
    def get_template_names(self, **kwargs):
        if self.kwargs['template_name']:
            return (self.kwargs['template_name'])
        else:
            return ('metrics.html')
            
    def get_context_data(self, **kwargs):
        users = User.objects
        users.today = users.filter(date_joined__range = (date_today(), now()))
        users.days7 = users.filter(date_joined__range = (date_today()-timedelta(days=7), now()))
        users.year = users.filter(date_joined__year = date_today().year)
        users.month = users.year.filter(date_joined__month = date_today().month)
        users.yesterday = users.filter(date_joined__range = (date_today()-timedelta(days=1), date_today()))
        users.gr = users.filter(profile__goodreads_user_id__isnull = False)
        users.lt = users.exclude(profile__librarything_id = '')
        users.fb = users.filter(profile__facebook_id__isnull = False)
        users.tw = users.exclude(profile__twitter_id = '')
        works = models.Work.objects
        works.today = works.filter(created__range = (date_today(), now()))
        works.days7 = works.filter(created__range = (date_today()-timedelta(days=7), now()))
        works.year = works.filter(created__year = date_today().year)
        works.month = works.year.filter(created__month = date_today().month)
        works.yesterday = works.filter(created__range = (date_today()-timedelta(days=1), date_today()))
        works.wishedby2 = works.filter(num_wishes__gte = 2)
        works.wishedby20 = works.filter(num_wishes__gte = 20)
        works.wishedby5 = works.filter(num_wishes__gte = 5)
        works.wishedby50 = works.filter(num_wishes__gte = 50)
        works.wishedby10 = works.filter(num_wishes__gte = 10)
        works.wishedby100 = works.filter(num_wishes__gte = 100)
        ebooks = models.Ebook.objects
        ebooks.today = ebooks.filter(created__range = (date_today(), now()))
        ebooks.days7 = ebooks.filter(created__range = (date_today()-timedelta(days=7), now()))
        ebooks.year = ebooks.filter(created__year = date_today().year)
        ebooks.month = ebooks.year.filter(created__month = date_today().month)
        ebooks.yesterday = ebooks.filter(created__range = (date_today()-timedelta(days=1), date_today()))
        wishlists= models.Wishlist.objects.exclude(wishes__isnull=True)
        wishlists.today = wishlists.filter(created__range = (date_today(), now()))
        wishlists.days7 = wishlists.filter(created__range = (date_today()-timedelta(days=7), now()))
        wishlists.year = wishlists.filter(created__year = date_today().year)
        wishlists.month = wishlists.year.filter(created__month = date_today().month)
        if date_today().day==1:
            wishlists.yesterday = wishlists.filter(created__range = (date_today()-timedelta(days=1), date_today()))
        else:
            wishlists.yesterday = wishlists.month.filter(created__day = date_today().day-1)
        return {
            'users': users, 
            'works': works,
            'ebooks': ebooks,
            'wishlists': wishlists,
        }

class InfoLangView(TemplateView):
    
    def get_template_names(self, **kwargs):
        if self.kwargs['template_name']:
            return (self.kwargs['template_name'])
        else:
            return ('languages.html')
            
    def get_context_data(self, **kwargs):
        languages=models.Work.objects.filter(num_wishes__gte = 1).values('language').annotate(lang_count=Count('language')).order_by('-lang_count')
        return {
            'wished_languages': languages, 
        }


class FAQView(TemplateView):
    template_name = "faq.html"
    def get_context_data(self, **kwargs):
        location = self.kwargs["location"]
        sublocation = self.kwargs["sublocation"]
        return {'location': location, 'sublocation': sublocation}

class GoodreadsDisplayView(TemplateView):
    template_name = "goodreads_display.html"
    def get_context_data(self, **kwargs):
        context = super(GoodreadsDisplayView, self).get_context_data(**kwargs)
        session = self.request.session
        gr_client = GoodreadsClient(key=settings.GOODREADS_API_KEY, secret=settings.GOODREADS_API_SECRET)
        
        user = self.request.user
        if user.is_authenticated():
            api_key = ApiKey.objects.filter(user=user)[0].key
            context['api_key'] = api_key

        if user.profile.goodreads_user_id is None:   
            # calculate the Goodreads authorization URL
            (context["goodreads_auth_url"], request_token) = gr_client.begin_authorization(self.request.build_absolute_uri(reverse('goodreads_cb')))
            logger.info("goodreads_auth_url: %s" %(context["goodreads_auth_url"]))
            # store request token in session so that we can redeem it for auth_token if authorization works
            session['goodreads_request_token'] = request_token['oauth_token']
            session['goodreads_request_secret'] = request_token['oauth_token_secret']
        else:
            gr_shelves = gr_client.shelves_list(user_id=user.profile.goodreads_user_id)
            context["shelves_info"] = gr_shelves
            gr_shelf_load_form = GoodreadsShelfLoadingForm()
            # load the shelves into the form
            choices = [('all:%d' % (gr_shelves["total_book_count"]),'all (%d)' % (gr_shelves["total_book_count"]))] +  \
                [("%s:%d" % (s["name"], s["book_count"]) ,"%s (%d)" % (s["name"],s["book_count"])) for s in gr_shelves["user_shelves"]]
            gr_shelf_load_form.fields['goodreads_shelf_name_number'].widget = Select(choices=tuple(choices))
            
            context["gr_shelf_load_form"] = gr_shelf_load_form
            
# also load any CeleryTasks associated with the user
            context["celerytasks"] = models.CeleryTask.objects.filter(user=user)
            
        return context

@login_required
def goodreads_auth(request):

    # calculate the Goodreads authorization URL
    gr_client = GoodreadsClient(key=settings.GOODREADS_API_KEY, secret=settings.GOODREADS_API_SECRET)
    (goodreads_auth_url, request_token) = gr_client.begin_authorization(request.build_absolute_uri(reverse('goodreads_cb')))
    logger.info("goodreads_auth_url: %s" %(goodreads_auth_url))
    # store request token in session so that we can redeem it for auth_token if authorization works
    request.session['goodreads_request_token'] = request_token['oauth_token']
    request.session['goodreads_request_secret'] = request_token['oauth_token_secret']
    
    return HttpResponseRedirect(goodreads_auth_url)

@login_required    
def goodreads_cb(request):
    """handle callback from Goodreads"""
    
    session = request.session
    authorized_flag = request.GET['authorize']  # is it '1'?
    request_oauth_token = request.GET['oauth_token']

    if authorized_flag == '1':
        request_token = {'oauth_token': session.get('goodreads_request_token'),
                         'oauth_token_secret': session.get('goodreads_request_secret')}
        gr_client = GoodreadsClient(key=settings.GOODREADS_API_KEY, secret=settings.GOODREADS_API_SECRET)
        
        access_token = gr_client.complete_authorization(request_token)
        
        # store the access token in the user profile
        profile = request.user.profile
        profile.goodreads_auth_token = access_token["oauth_token"]
        profile.goodreads_auth_secret = access_token["oauth_token_secret"]
    
        # let's get the userid, username
        user = gr_client.auth_user()
        
        profile.goodreads_user_id = user["userid"]
        profile.goodreads_user_name = user["name"]
        profile.goodreads_user_link = user["link"]
        
        profile.save()  # is this needed?

    # redirect to the Goodreads display page -- should observe some next later
    return HttpResponseRedirect(reverse('home'))

@require_POST
@login_required
@csrf_exempt    
def goodreads_flush_assoc(request):
    user = request.user
    if user.is_authenticated():
        profile = user.profile
        profile.goodreads_user_id = None
        profile.goodreads_user_name = None
        profile.goodreads_user_link = None
        profile.goodreads_auth_token = None
        profile.goodreads_auth_secret = None
        profile.save()
        logger.info('Goodreads association flushed for user %s', user)
    return HttpResponseRedirect(reverse('goodreads_display'))
      
@require_POST
@login_required      
@csrf_exempt
def goodreads_load_shelf(request):
    """
    a view to allow user load goodreads shelf into her wishlist
    """
    # Should be moved to the API
    goodreads_shelf_name_number = request.POST.get('goodreads_shelf_name_number', 'all:0')
    user = request.user
    try:
        # parse out shelf name and expected number of books
        (shelf_name, expected_number_of_books) = re.match(r'^(.*):(\d+)$', goodreads_shelf_name_number).groups()
        expected_number_of_books = int(expected_number_of_books)
        logger.info('Adding task to load shelf %s to user %s with %d books', shelf_name, user, expected_number_of_books)
        load_task_name = "load_goodreads_shelf_into_wishlist"
        load_task = getattr(tasks, load_task_name)
        task_id = load_task.delay(user.id, shelf_name, expected_number_of_books=expected_number_of_books)
        
        ct = models.CeleryTask()
        ct.task_id = task_id
        ct.function_name = load_task_name
        ct.user = user
        ct.description = "Loading Goodread shelf %s to user %s with %s books" % (shelf_name, user, expected_number_of_books)
        ct.save()
        
        return HttpResponse("<span style='margin: auto 10px auto 36px;vertical-align: middle;display: inline-block;'>We're on it! <a href='JavaScript:window.location.reload()'>Reload the page</a> to see the books we've snagged so far.</span>")
    except Exception,e:
        return HttpResponse("Error in loading shelf: %s " % (e))
        logger.info("Error in loading shelf for user %s: %s ", user, e)


@login_required
def goodreads_calc_shelves(request):

    # we should move towards calculating this only if needed (perhaps with Ajax), caching previous results, etc to speed up
    # performance
    
    if request.user.profile.goodreads_user_id is not None:
        gr_client = GoodreadsClient(key=settings.GOODREADS_API_KEY, secret=settings.GOODREADS_API_SECRET)
        goodreads_shelves = gr_client.shelves_list(user_id=request.user.profile.goodreads_user_id)
        #goodreads_shelf_load_form = GoodreadsShelfLoadingForm()
        ## load the shelves into the form
        #choices = [('all:%d' % (goodreads_shelves["total_book_count"]),'all (%d)' % (goodreads_shelves["total_book_count"]))] +  \
        #    [("%s:%d" % (s["name"], s["book_count"]) ,"%s (%d)" % (s["name"],s["book_count"])) for s in goodreads_shelves["user_shelves"]]
        #goodreads_shelf_load_form.fields['goodreads_shelf_name_number'].widget = Select(choices=tuple(choices))
    else:
        goodreads_shelf_load_form = None
    
    return HttpResponse(json.dumps(goodreads_shelves), content_type="application/json")
    

@require_POST
@login_required      
@csrf_exempt
def librarything_load(request):
    """
    a view to allow user load librarything library into her wishlist
    """
    # Should be moved to the API
    user = request.user

    
    try:        
        # figure out expected_number_of_books later
        
        lt_username = request.user.profile.librarything_id
        logger.info('Adding task to load librarything %s to user %s', lt_username, user )
        load_task_name = "load_librarything_into_wishlist"
        load_task = getattr(tasks, load_task_name)
        task_id = load_task.delay(user.id, lt_username, None)
        
        ct = models.CeleryTask()
        ct.task_id = task_id
        ct.function_name = load_task_name
        ct.user = user
        ct.description = "Loading LibraryThing collection of %s to user %s." % (lt_username, user)
        ct.save()
            
        return HttpResponse("<span style='margin: auto 10px auto 36px;vertical-align: middle;display: inline-block;'>We're on it! <a href='JavaScript:window.location.reload()'>Reload the page</a> to see the books we've snagged so far.</span>")
    except Exception,e:
        return HttpResponse("Error in loading LibraryThing library: %s " % (e))
        logger.info("Error in loading LibraryThing for user %s: %s ", user, e)

@require_POST
@login_required      
@csrf_exempt
def clear_wishlist(request):
    try:
        request.user.wishlist.works.clear()
        logger.info("Wishlist for user %s cleared", request.user)
        return HttpResponse('wishlist cleared')
    except Exception, e:
        return HttpResponse("Error in clearing wishlist: %s " % (e))
        logger.info("Error in clearing wishlist for user %s: %s ", request.user, e)
    

class LibraryThingView(FormView):
    template_name="librarything.html"
    form_class = LibraryThingForm
    
    def get_context_data(self, **kwargs):
        context = super(LibraryThingView, self).get_context_data(**kwargs)
        form = kwargs['form']
        # get the books for the lt_username in the form
        lt_username=self.request.GET.get("lt_username",None)
        if lt_username is not None:
            lt = librarything.LibraryThing(username=lt_username)
            context.update({'books':list(lt.parse_user_catalog(view_style=5))})
        else:
            context.update({'books':None})
            
        # try picking up the LibraryThing api key -- and set to None if not available.  Not being used for
        # anything crucial at this moment, so a None is ok here
        try:
            context.update({'lt_api_key':settings.LIBRARYTHING_API_KEY})
        except:
            pass
        
        return context

    def form_valid(self,form):
        return super(LibraryThingView, self).form_valid(form)
    
@require_POST
@login_required      
@csrf_exempt
def clear_celery_tasks(request):
    try:
        request.user.tasks.clear()
        logger.info("Celery tasks for user %s cleared", request.user)
        return HttpResponse('Celery Tasks List cleared')
    except Exception, e:
        return HttpResponse("Error in clearing Celery Tasks: %s " % (e))
        logger.info("Error in clearing Celery Tasks for user %s: %s ", request.user, e)    

def celery_test(request):
    return HttpResponse("celery_test")

# routing views that try to redirect to the works page on a 3rd party site
#
# TODO: need to queue up a task to look up IDs if we have to fallback to 
# routing based on ISBN or search

def work_librarything(request, work_id):
    work = get_object_or_404(models.Work, id=work_id)
    isbn = work.first_isbn_13()
    if work.librarything_id:
        url = work.librarything_url
    elif isbn:
        # TODO: do the redirect here and capture the work id?
        url = "http://www.librarything.com/isbn/%s" % isbn
    else:
        term = work.title + " " + work.author()
        q = urlencode({'searchtpe': 'work', 'term': term})
        url = "http://www.librarything.com/search.php?" + q
    return HttpResponseRedirect(url)

def work_openlibrary(request, work_id):
    work = get_object_or_404(models.Work, id=work_id)
    isbns = ["ISBN:" + i.value for i in work.identifiers.filter(type='isbn')]
    url = None

    if work.openlibrary_id:
        url = work.openlibrary_url
    elif len(isbns) > 0:
        isbns = ",".join(isbns)
        u = 'http://openlibrary.org/api/books?bibkeys=%s&jscmd=data&format=json' % isbns
        try:
            j = json.loads(requests.get(u).content)
            # as long as there were some matches get the first one and route to it
            if len(j.keys()) > 0:
                first = j.keys()[0]
                url = "http://openlibrary.org" + j[first]['key'] 
        except ValueError:
            # fail at openlibrary
            logger.warning("failed to get OpenLibrary json at %s" % u)          
    # fall back to doing a search on openlibrary
    if not url:
        q = urlencode({'q': work.title + " " + work.author()})
        url = "http://openlibrary.org/search?" + q
    return HttpResponseRedirect(url)

def work_goodreads(request, work_id):
    work = get_object_or_404(models.Work, id=work_id)
    isbn = work.first_isbn_13()
    if work.goodreads_id:
        url = work.goodreads_url
    elif isbn:
        url = "http://www.goodreads.com/book/isbn/%s" % isbn
    else:
        q = urlencode({'query': work.title + " " + work.author()})
        url = "http://www.goodreads.com/search?" + q
    return HttpResponseRedirect(url)

@login_required
def emailshare(request, action):
    if request.method == 'POST':
        form=EmailShareForm(request.POST)
        if form.is_valid():
            subject = form.cleaned_data['subject']
            message = form.cleaned_data['message']
            sender = '%s via Unglue.it <%s>'%(request.user.username, request.user.email)
            recipient = form.cleaned_data['recipient']
            send_mail_task.delay(subject, message, sender, [recipient])
            try:
                next = form.cleaned_data['next']
            except:
                next = ''
            return HttpResponseRedirect(next)
            
    else:
        
        try:
            next = request.GET['next']
            work_id = next.split('/')[-2]
            work_id = int(work_id)
            work = models.Work.objects.get(pk=work_id)
            if action == 'pledge':
                message = render_to_string('emails/i_just_pledged.txt',{'request':request,'work':work,'site': Site.objects.get_current()})
                subject = "Help me unglue "+work.title
            else:
                try:
                    status = work.last_campaign().status
                except:
                    status = None
            
                # customize the call to action depending on campaign status
                if status == 'ACTIVE':
                    message = render_to_string('emails/pledge_this.txt',{'request':request,'work':work,'site': Site.objects.get_current()})
                else:
                    message = render_to_string('emails/wish_this.txt',{'request':request,'work':work,'site': Site.objects.get_current()})
                subject = 'Come see one of my favorite books on Unglue.it'
            
            form = EmailShareForm(initial={ 'next':next, 'subject': subject, 'message': message})
        except:
            next = ''
            form = EmailShareForm(initial={'next':next, 'subject': 'Come join me on Unglue.it', 'message':render_to_string('emails/join_me.txt',{'request':request,'site': Site.objects.get_current()})})

    return render(request, "emailshare.html", {'form':form})    
    
def feedback(request):
    num1 = randint(0,10)
    num2 = randint(0,10)
    sum = num1 + num2
    
    if request.method == 'POST':
        form=FeedbackForm(request.POST)
        if form.is_valid():
            subject = form.cleaned_data['subject']
            message = form.cleaned_data['message']
            sender = form.cleaned_data['sender']
            recipient = 'support@gluejar.com'
            page = form.cleaned_data['page']
            useragent = request.META['HTTP_USER_AGENT']
            if request.user.is_anonymous():
                ungluer = "(not logged in)"
            else:
                ungluer = request.user.username
            message = "<<<This feedback is about "+page+". Original user message follows\nfrom "+sender+", ungluer name "+ungluer+"\nwith user agent "+useragent+"\n>>>\n"+message
            send_mail_task.delay(subject, message, sender, [recipient])
            
            return render(request, "thanks.html", {"page":page}) 
            
        else:
            num1 = request.POST['num1']
            num2 = request.POST['num2']
        
    else:
        if request.user.is_authenticated():
            sender=request.user.email;
        else:
            sender=''
        try:
            page = request.GET['page']
        except:
            page='/'
        form = FeedbackForm(initial={"sender":sender, "subject": "Feedback on page "+page, "page":page, "num1":num1, "num2":num2, "answer":sum})
        
    return render(request, "feedback.html", {'form':form, 'num1':num1, 'num2':num2})    
        
def comment(request):
    latest_comments = Comment.objects.all().order_by('-submit_date')[:20]
    return render(request, "comments.html", {'latest_comments': latest_comments})

def campaign_archive_js(request):
    """ proxy for mailchimp js"""
    response = HttpResponse()
    r = requests.get(settings.CAMPAIGN_ARCHIVE_JS)
    response.status_code = r.status_code
    response.content = r.content
    response["Content-Type"] = "text/javascript"
    return response

def lockss(request, work_id):
    """
    manifest pages for lockss harvester -- individual works
    """
    work = safe_get_work(work_id)
    try:
        ebooks = work.ebooks().filter(edition__unglued=True)
    except:
        ebooks = None
    authors = list(models.Author.objects.filter(editions__work=work).all())
    
    return render(request, "lockss.html", {'work':work, 'ebooks':ebooks, 'authors':authors})
    
def lockss_manifest(request, year):
    """
    manifest pages for lockss harvester -- yearly indices
    (lockss needs pages listing all books unglued by year, with 
    programmatically determinable URLs)
    """
    year = int(year)
    start_date = date(year, 1, 1)
    end_date = date(year, 12, 31)
    try:
        ebooks = models.Edition.objects.filter(unglued=True).filter(created__range=(start_date, end_date))
    except:
        ebooks = None
    
    return render(request, "lockss_manifest.html", {'ebooks':ebooks, 'year': year})
    
def download(request, work_id):
    context = {}
    work = safe_get_work(work_id)
    context.update({'work': work})

    unglued_ebooks = work.ebooks().filter(edition__unglued=True)
    other_ebooks = work.ebooks().filter(edition__unglued=False)
    try:
        readmill_epub_url = work.ebooks().filter(format='epub').exclude(provider='Google Books')[0].url
    except:
        readmill_epub_url = None
        
    context.update({
        'unglued_ebooks': unglued_ebooks,
        'other_ebooks': other_ebooks,
        'readmill_epub_url': readmill_epub_url,
        'base_url': settings.BASE_URL
    })
    
    return render(request, "download.html", context)
    
def about(request, facet):
    template = "about_" + facet + ".html"
    return render(request, template)

