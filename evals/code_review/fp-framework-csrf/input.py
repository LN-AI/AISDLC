from django.shortcuts import render
from django.contrib.auth.decorators import login_required


@login_required
def profile(request):
    if request.method == "POST":
        request.user.bio = request.POST["bio"]
        request.user.save()
    return render(request, "profile.html")
