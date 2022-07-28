import csv
from datetime import datetime

from gsoc import settings

from .forms import AcceptanceForm, ChangeInfoForm, ProposalUploadForm
from .models import (
    RegLink,
    ProposalTextValidator,
    Comment,
    ArticleReview,
    GsocYear,
    ReaddUser,
    UserProfile,
)

import io
import os
import urllib
import json
import uuid

from django.contrib import messages
from django.contrib.auth import decorators, password_validation, validators, logout
from django.contrib.auth.models import User
from django.contrib.auth.forms import PasswordChangeForm
from django import shortcuts
from django.http import JsonResponse, HttpResponseRedirect
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.shortcuts import redirect
from django.urls import reverse
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.cache import never_cache
from django.db import IntegrityError

from aldryn_newsblog.models import Article

from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer.converter import TextConverter
from pdfminer.layout import LAParams
from pdfminer.pdfpage import PDFPage

from profanityfilter import ProfanityFilter

import google_auth_oauthlib.flow


ROLES = {1: 'Admin', 2: 'Mentor', 3: 'Student'}


# handle file upload


@csrf_exempt
def upload_file(request):
    file = request.FILES["upload"]
    filename = str(uuid.uuid4()) + "." + file.name.split(".")[-1]
    filepath = os.path.join("media/uploads", filename)
    fileurl = os.path.join("/", filepath)
    abspath = os.path.join(settings.BASE_DIR, filepath)
    if not os.path.exists(os.path.dirname(abspath)):
        os.makedirs(os.path.dirname(abspath))

    with open(abspath, "wb+") as destination:
        for chunk in file.chunks():
            destination.write(chunk)

    return JsonResponse({"uploaded": 1, "fileName": filename, "url": fileurl})


# handle redirect to blogs


def redirect_blogs_list(request):
    return HttpResponseRedirect(f"/")


def redirect_blogs(request, blog_name):
    return HttpResponseRedirect(f"/{blog_name}/")


def redirect_articles(request, blog_name, article_name):
    return HttpResponseRedirect(f"/{blog_name}/{article_name}/")


# handle proposal upload


def convert_pdf_to_txt(f):
    rsrcmgr = PDFResourceManager()
    retstr = io.StringIO()
    laparams = LAParams()
    device = TextConverter(rsrcmgr, retstr, codec="utf-8", laparams=None)
    interpreter = PDFPageInterpreter(rsrcmgr, device)
    pagenos = set()
    for page in PDFPage.get_pages(
        f, pagenos, maxpages=0, caching=True, check_extractable=True
    ):
        interpreter.process_page(page)
    text = retstr.getvalue()
    f.close()
    device.close()
    retstr.close()
    return text


def is_user_accepted_student(user):
    return user.is_current_year_student()


def is_superuser(user):
    return user.is_superuser


def scan_proposal(file):
    """
    NOTE: returns True if not found private data.
    """
    try:
        text = convert_pdf_to_txt(file)
    except BaseException:
        text = ""
    try:
        v = ProposalTextValidator()
        v.validate(text)
        return None
    except ValidationError as err:
        return err


@decorators.login_required
def after_login_view(request):
    user = request.user
    return shortcuts.redirect("/myprofile")


@decorators.login_required
@decorators.user_passes_test(is_user_accepted_student)
def upload_proposal_view(request):
    resp = {
        "private_data": {"emails": [], "possible_phone_numbers": [], "locations": []},
        "file_type_valid": False,
        "file_not_too_large": False,
    }
    if request.method == "POST":
        file = request.FILES.get("accepted_proposal_pdf")
        resp["file_type_valid"] = file and file.name.endswith(".pdf")
        if len(file.name) > 100 and resp["file_type_valid"]:
            file.name = str(uuid.uuid4()) + ".pdf"
            print(file.name)
        resp["file_type_valid"] = file and file.name.endswith(".pdf")
        resp["file_not_too_large"] = file.size < 20 * 1024 * 1024
        if resp["file_type_valid"] and resp["file_not_too_large"]:
            profile = request.user.student_profile()
            form = ProposalUploadForm(request.POST, request.FILES, instance=profile)
            if form.is_valid():
                form.save()
                scan_result = scan_proposal(file)
                if scan_result:
                    resp["private_data"] = scan_result.message_dict
    return JsonResponse(resp)


@decorators.login_required
@decorators.user_passes_test(is_user_accepted_student)
def cancel_proposal_upload_view(request):
    profile = request.user.student_profile()
    profile.accepted_proposal_pdf.delete()
    return shortcuts.HttpResponse()


@decorators.login_required
@decorators.user_passes_test(is_user_accepted_student)
def confirm_proposal_view(request):
    profile = request.user.student_profile()
    if profile.accepted_proposal_pdf:
        profile.confirm_proposal()
    return shortcuts.HttpResponse()


def new_account_view(request):
    if request.method == "POST":
        email = request.POST.get("email", None)
        gsoc_year = GsocYear.objects.first()
        if email:
            RegLink.objects.create(user_role=0, gsoc_year=gsoc_year, email=email)
            messages.success(
                request, "You will get the registration link sent to your email soon"
            )
        else:
            messages.error(request, "An error occured, try again!")
        return shortcuts.redirect("/")
    return shortcuts.render(request, "registration/new_account.html")


def register_view(request):

    reglink_id = request.GET.get("reglink_id", request.POST.get("reglink_id", ""))
    try:
        reglink = RegLink.objects.get(reglink_id=reglink_id)
        reglink_usable = reglink.is_usable()
    except RegLink.DoesNotExist:
        reglink_usable = False
        reglink = None
    context = {
        "can_register": True,
        "done_registration": False,
        "warning": "",
        "reglink_id": reglink_id,
        "email": getattr(reglink, "email", "EMPTY"),
    }

    if request.user.is_authenticated:
        try:
            profile = UserProfile.objects.get(
                user=request.user,
                gsoc_year=datetime.now().year,
            )
            messages.info(
                request,
                f"Registered as {ROLES.get(profile.role)} with " +
                f"{profile.suborg_full_name} x please login again"
            )
        except UserProfile.DoesNotExist:
            messages.info(request, "You have been logged out.")
        logout(request)

    try:
        if reglink_usable is False or request.method == "GET":
            user = User.objects.filter(email=context["email"]).first()
            if user:
                if reglink.is_used:
                    messages.info(request, "Invitaion already accepted!!")
                    return shortcuts.redirect("/")

                messages.info(
                    request,
                    f"Please enter your credentials " +
                    f"to accept invitation " +
                    f"of {ROLES.get(reglink.user_role)} to {reglink.user_suborg}.",
                )
                form = AcceptanceForm(initial={
                    'email': reglink.email,
                    })
                data = {'form': form, 'reglink': reglink_id}
                return shortcuts.render(request, "registration/acceptance.html", data)

            if reglink_usable is False:
                context["can_register"] = False
                context[
                    "warning"
                ] = "Your registration link is invalid! Please check again!"
            return shortcuts.render(request, "registration/register.html", context)
    except IntegrityError:
        context["can_register"] = False
        context[
            "warning"
        ] = "Your registration link has already been used!"
        return shortcuts.render(request, "registration/register.html", context)
    if request.method == "POST":
        username = request.POST.get("username", "")
        password = request.POST.get("password", "")
        password2 = request.POST.get("password2", "")
        github_handle = request.POST.get("github_handle", "")
        email_opt_in = request.POST.get("email_opt_in")
        reminder_disabled = False if email_opt_in == "on" else True
        info_valid = True
        registration_success = True
        if password != password2:
            context["warning"] += "Your password didn't match! <BR>"
            info_valid = False
        try:
            User.objects.get(username=username)
            info_valid = False
            context["warning"] += "Your username has been used!<br>"
        except User.DoesNotExist:
            pass

        # Check password
        try:
            password_validation.validate_password(password)
        except ValidationError as e:
            context["warning"] += f'{"<br>".join(e.messages)}<BR>'
            info_valid = False
        try:
            validators.UnicodeUsernameValidator()(username)
        except ValidationError as e:
            context["warning"] += f'{"<br>".join(e.messages)}<BR>'
            info_valid = False

        if info_valid:
            try:
                user = reglink.create_user(
                    username=username,
                    reminder_disabled=reminder_disabled,
                    github_handle=github_handle,
                )
                user.set_password(password)
                user.save()
            except Exception:
                user = None
        else:
            user = None

        if user is None:
            registration_success = False
        if registration_success:
            reglink.is_used = True
            reglink.save()
            context["done_registration"] = True
            context["warning"] = ""
        else:
            context["done_registration"] = False

        return shortcuts.render(request, "registration/register.html", context)


def accept_invitation(request):
    if request.method == 'POST':
        form = AcceptanceForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']
            password = form.cleaned_data['password']
            reglink_id = form.cleaned_data['reglink']
            try:
                reglink = RegLink.objects.get(reglink_id=reglink_id)
                user = User.objects.get(email=email)
                if email == reglink.email:
                    if user.check_password(password):
                        reglink.create_user(username=user.username)
                        reglink.is_used = True
                        reglink.save()
                        messages.success(request, "Invitaion accepted successfully!!")
                        return shortcuts.redirect("/")
                    else:
                        messages.error(request, "Invalid credentials. Please try again.")
                else:
                    messages.error(request, "Invalid email for the reglink.")
            except User.DoesNotExist:
                messages.error(request, "Invalid email provided.")
        else:
            messages.info(request, "Something went wrong. Please try again later.")
        return shortcuts.redirect(request.META.get('HTTP_REFERER', '/'))


@decorators.login_required
def change_password(request):
    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Your password was successfully updated!")
            return redirect("change_password")
        else:
            messages.error(request, "Please correct the error below.")
    else:
        form = PasswordChangeForm(request.user)

    return shortcuts.render(
        request, "registration/change_password.html", {"form": form}
    )


@decorators.login_required
def change_info(request):
    if request.method == "POST":
        form = ChangeInfoForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile information Updated successfully!")
            return redirect("change_info")
        else:
            messages.error(request, "Please correct the error below.")
    else:
        form = ChangeInfoForm(instance=request.user)

    return shortcuts.render(
        request, "registration/change_info.html", {"form": form}
    )

@never_cache
def new_comment(request):
    if request.method == "POST":
        # set environment variable `DISABLE_RECAPTCHA` to disable recaptcha
        # verification and delete the variable to enable recaptcha verification
        disable_recaptcha = os.getenv("DISABLE_RECAPTCHA", None)

        flag = True
        if not disable_recaptcha:
            recaptcha_response = request.POST.get("g-recaptcha-response")
            url = "https://www.google.com/recaptcha/api/siteverify"
            payload = {
                "secret": settings.RECAPTCHA_PRIVATE_KEY,
                "response": recaptcha_response,
            }
            data = urllib.parse.urlencode(payload).encode()
            req = urllib.request.Request(url, data=data)

            response = urllib.request.urlopen(req)
            result = json.loads(response.read().decode())

            flag = result["success"]

        if flag:
            # if score greater than threshold allow to add
            comment = request.POST.get("comment")
            article_pk = request.POST.get("article")
            article = Article.objects.get(pk=article_pk)
            user_pk = request.POST.get("user", None)
            parent_pk = request.POST.get("parent", None)

            if parent_pk:
                parent = Comment.objects.get(pk=parent_pk)
            else:
                parent = None

            if user_pk:
                user = User.objects.get(pk=user_pk)
                username = user.username
            else:
                user = None
                username = request.POST.get("username")

            pf = ProfanityFilter()
            if pf.is_clean(comment) and pf.is_clean(username):
                c = Comment(
                    username=username,
                    content=comment,
                    user=user,
                    article=article,
                    parent=parent,
                )
                c.save()
            else:
                messages.add_message(
                    request,
                    messages.ERROR,
                    "Abusive content detected! Please refrain\
                                      from using any indecent words while commenting.",
                )
        else:
            messages.add_message(
                request, messages.ERROR, "reCAPTCHA verification failed."
            )

        redirect_path = request.POST.get("redirect")

        cache.clear()

        # mem = MemcachedStats()
        # keys = [_[3:] for _ in mem.keys()]
        # for key in keys:
        #     if 'cache_page' in key or 'cache_header' in key:
        #         print(key, cache.get(key))
        #         cache.delete(key)

        if redirect_path:
            return redirect(redirect_path)
        else:
            return redirect("/")


@decorators.user_passes_test(is_superuser)
def delete_comment(request):
    if request.method == "POST":
        pk = request.POST.get("comment_pk")
        redirect_path = request.POST.get("redirect")

        if pk:
            comment = Comment.objects.get(pk=pk)
            comment.delete()

        if redirect_path:
            return redirect(redirect_path)
        else:
            return redirect("/")


@decorators.user_passes_test(is_superuser)
def review_article(request, article_id):
    if request.method == "GET":
        a = Article.objects.get(id=article_id)
        try:
            ar = ArticleReview.objects.get(article=a)
            ar.is_reviewed = True
            ar.last_reviewed_by = request.user
            ar.save()
        except ArticleReview.DoesNotExist:
            pass
        admin_request = request.GET.get("admin")
        if admin_request == "true":
            return redirect(reverse("admin:gsoc_articlereview_change", args=[ar.id]))
    return redirect(
        reverse("{}:article-detail".format(a.app_config.namespace), args=[a.slug])
    )


@decorators.login_required
def unpublish_article(request, article_id):
    if request.method == "GET":
        a = Article.objects.get(id=article_id)
        if request.user == a.owner or request.user.is_superuser:
            a.is_published = False
            a.save()
        else:
            messages.error(
                request, "User does not have permission to unpublish article"
            )
    return redirect(
        reverse("{}:article-detail".format(a.app_config.namespace), args=[a.slug])
    )


@decorators.login_required
def publish_article(request, article_id):
    if request.method == "GET":
        a = Article.objects.get(id=article_id)
        if request.user == a.owner or request.user.is_superuser:
            a.is_published = True
            a.save()
        else:
            messages.error(request, "User does not have permission to publish article")
    return redirect(
        reverse("{}:article-detail".format(a.app_config.namespace), args=[a.slug])
    )


def readd_users(request, uuid):
    if request.method == "GET":
        readds = ReaddUser.objects.filter(uuid=uuid)
        email = request.GET.get("email")
        context = {"success": False}
        if len(readds) > 0:
            readd = readds.first()
            if email:
                readd.readd_user_details(email)
                context = {"success": True}
            else:
                messages.error("Please provide your email")
        else:
            messages.error("Incorrect token, please use the correct token")

    return shortcuts.render(request, "readd.html", context)


def csrf_failure(request, reason="CSRF failed"):
    if request.user.is_authenticated:
        return shortcuts.redirect('/')
    messages.info(request, "CSRF Token verification failed.")
    return shortcuts.redirect('accounts/login')


# Export mentors view
@decorators.login_required
@decorators.user_passes_test(is_superuser)
def export_view(request):
    if request.method == "GET":
        return HttpResponse(
            "<div style='padding:60px'>"
            "<h1>Mentors data exported successfully!!</h1>" +
            "<a href='admin/export'>Click here to download</a>" +
            "</div>"
        )


@decorators.login_required
@decorators.user_passes_test(is_superuser)
def export_mentors(request):
    output = []
    ROLES = {1: 'Suborg Admin', 2: 'Mentor'}
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="Mentors.csv"'
    writer = csv.writer(response)
    query_set = UserProfile.objects.filter(
        gsoc_year=datetime.now().year,
        role__in=[2, 1]
        ).order_by("-id")

    writer.writerow(['User', 'Email', 'Suborg', 'Role'])
    for userprofile in query_set:
        output.append([
            userprofile.user,
            userprofile.user.email,
            userprofile.suborg_full_name,
            ROLES.get(userprofile.role)
            ])
    writer.writerows(output)

    return response


from django.http import HttpResponse


def test(request):
    return HttpResponse("{}".format(request.META["REMOTE_ADDR"]))


# Google OAuth
SCOPES = ['https://www.googleapis.com/auth/calendar']
CLIENT_SECRETS_FILE = os.path.join(settings.BASE_DIR, 'credentials.json')


@decorators.login_required
@decorators.user_passes_test(is_superuser)
def authorize(request):
    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES
    )

    flow.redirect_uri = settings.OAUTH_REDIRECT_URI + "oauth2callback"

    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true'
    )

    request.session['state'] = state

    return redirect(authorization_url)


@decorators.login_required
@decorators.user_passes_test(is_superuser)
def oauth2callback(request):
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

    state = request.session.get('state')

    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        state=state
    )
    flow.redirect_uri = settings.OAUTH_REDIRECT_URI + "oauth2callback"

    authorization_response = request.get_full_path()
    flow.fetch_token(authorization_response=authorization_response)

    credentials = flow.credentials
    with open(os.path.join(settings.BASE_DIR, 'token.json'), 'w') as token:
        token.write(credentials.to_json())

    return HttpResponse("Token generated successfully!!")


@decorators.login_required
@decorators.user_passes_test(is_superuser)
def mark_all_article_as_reviewed(request, author_id):
    user = User.objects.get(id=author_id)
    current_year = datetime.now().year
    articles = Article.objects.filter(
        owner=user,
        publishing_date__contains=current_year
    )
    for article in articles:
        try:
            review = ArticleReview.objects.get(
                article=article,
                is_reviewed=False
            )
            review.is_reviewed = True
            review.last_reviewed_by = request.user
            review.save()
        except Exception:
            pass

    messages.success(request, "All articles marked as reviewed!")
    return HttpResponseRedirect(request.META.get('HTTP_REFERER'))
