SELECT E.Lname
FROM EMPLOYEE E JOIN WORKS_ON W ON E.Ssn = W.Ssn
     JOIN PROJECT P ON P.Pnumber = W.Pno
WHERE P.Pname = 'Aquarius' AND E.Bdate > '1957-12-31';
