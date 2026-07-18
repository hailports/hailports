export default {
  fetch(request) {
    const url = new URL(request.url);
    url.hostname = "www.hailports.com";
    return Response.redirect(url.toString(), 308);
  },
};
